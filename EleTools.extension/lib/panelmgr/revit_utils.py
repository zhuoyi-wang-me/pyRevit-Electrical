# -*- coding: utf-8 -*-
"""
revit_utils.py
==============
The ONLY module (besides the pushbutton script) that is allowed to
import the Revit API directly. Everything here is a thin, defensive
wrapper: read data into the plain `models.py` classes, or push
plain-model changes back into Revit elements. Keeping this boundary
lets `services/*` stay unit-testable without Revit running.

Revit API references used (verified against Autodesk's published API
documentation / "API Changes" notes rather than assumed):
    Autodesk.Revit.DB
    Autodesk.Revit.DB.Electrical
        ElectricalSystem, PanelScheduleView

IMPORTANT CORRECTION: an earlier revision of this file referenced a
non-existent `Autodesk.Revit.DB.Electrical.SpareSpaceInfo` /
`SpareSpaceType` class and an `AddSpareOrSpace` / `Rearrange` method.
Those do not exist in the real API and caused an ImportException at
load time. The ACTUAL PanelScheduleView members for this purpose
(present since the 2018.1 API changes and documented on
thebuildingcoder.typepad.com) are:
    AddSpace(int slotNumber, int slotSequence)
    AddSpare(int slotNumber, int slotSequence)
    RemoveSpace(int slotNumber, int slotSequence)
    RemoveSpare(int slotNumber, int slotSequence)
    IsSpace(int slotNumber, int slotSequence) -> bool
    IsSpare(int slotNumber, int slotSequence) -> bool
    IsSlotGrouped(int slotNumber) -> int
    CanMoveSlotTo(int fromSlot, int fromSeq, int toSlot, int toSeq) -> bool
    MoveSlotTo(int fromSlot, int fromSeq, int toSlot, int toSeq)
    GetCellsBySlotNumber(int slotNumber)
    GetPanel() -> ElementId
`slotSequence` addresses the pole position within a slot (1 for the
first/only pole of a single-pole breaker occupying that slot number).
This module has been rewritten below to use only these confirmed
members.

SECOND CORRECTION (found from a live-run traceback: "The given row
number nRow is invalid in Body."): `AddSpace`/`AddSpare`/`IsSpace`/
`IsSpare`/`MoveSlotTo` all take a `slotNumber` that Revit maps
internally onto a row in the panel schedule's Body table SECTION -
and that row count is NOT guaranteed to equal the panel family's
"Slots" / "Number of Poles/Slots" instance parameter (the schedule
template controls how many rows actually exist; it does not have to
mirror the enclosure's physical pole capacity 1:1). Calling these
methods with a slot number beyond the schedule's real row count
throws that exception. The fix is `get_schedule_body_row_count()`
below, which reads the TRUE bound via
`PanelScheduleView.GetTableData().GetSectionData(SectionType.Body)
.NumberOfRows` and is used everywhere a slot count is needed instead
of trusting the family parameter alone.
THIRD CORRECTION (found by cross-checking against a proven, previously
working pyRevit extension - SpareSpaceFiller - that a colleague
shared): `AddSpare` / `AddSpace` do NOT take a linear "slot number" at
all. They take a **(row, column)** cell address into the schedule's
Body table, exactly as returned by
`PanelScheduleView.GetCellsBySlotNumber(slotNumber)` (which returns
parallel `row[]`/`col[]` arrays - use `row[0], col[0]` for the primary
cell). This is what SECOND CORRECTION above was dancing around
without quite landing on: passing a raw 1..N slot number directly as
the first argument only "worked" by coincidence for single-column
schedules or low slot numbers, and broke for anything using a
two-column layout or a slot number beyond the Body table's actual row
count - which matches the exact traceback that prompted SECOND
CORRECTION. Every function in this module that touches a slot now
resolves `slot -> (row, col)` via `GetCellsBySlotNumber` FIRST, then
calls the real Revit method with that (row, col) pair. The same
proven extension also confirmed:
    - `PanelScheduleView.GetCircuitByCell(row, col)` returns the
      circuit/spare/space occupying a cell (or None if empty) - this
      is the reliable way to check "is this slot empty", not IsSpare/
      IsSpace (whose exact signature we were never able to verify).
    - `PanelScheduleView.GetTableData().NumberOfSlots` gives the
      schedule's actual slot count directly - simpler and more
      reliable than deriving it from the Body section's row count.
    - Spare breaker rating is `BuiltInParameter
      .RBS_ELEC_CIRCUIT_RATING_PARAM` and pole count is
      `BuiltInParameter.RBS_ELEC_NUMBER_OF_POLES`, both read off the
      circuit object returned by `GetCircuitByCell` immediately after
      `AddSpare`/`AddSpace`.
    - Physical adjacency for multi-pole spare/space placement must be
      checked as genuine consecutive ROWS in the SAME COLUMN (how a
      breaker physically stacks), not consecutive slot numbers, once a
      panel has a mix of pole counts already in it.
See `fill_spare_and_space` below, which ports this proven approach.
"""

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter,
    Transaction, TransactionGroup, SubTransaction, ElementId, StorageType,
    SectionType, IFailuresPreprocessor, FailureProcessingResult, FailureSeverity,
    TransactionStatus,
)
from Autodesk.Revit.DB.Electrical import (
    PanelScheduleView, PanelScheduleTemplate
)

from panelmgr.models import (
    PanelInfo, CircuitInfo, SINGLE_PHASE, THREE_PHASE
)
from panelmgr.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# ElementId 32-bit / 64-bit compatibility
# ---------------------------------------------------------------------------
# THIRTY-EIGHTH CORRECTION. This codebase stored and reconstructed
# every Revit id through `ElementId.IntegerValue` and `ElementId(int)`.
# Both were deprecated in the Revit 2024 API in favour of
# `ElementId.Value` / `ElementId(Int64)`, are still only obsolete (not
# removed) in 2025, and are GONE in 2026+. Two concrete consequences,
# not just a style warning:
#
#   1. `IntegerValue` raises InvalidOperationException when an id is
#      too large to fit in 32 bits. Ids that big are normal in large
#      and cloud-hosted models, so on those projects the tool throws
#      while merely READING a panel - before it has done anything.
#   2. On Revit 2026 the attribute does not exist at all, so the
#      extension cannot load even though extension.json only claims a
#      MINIMUM version of 2025.
#
# These two helpers prefer the modern API and fall back to the legacy
# one, so the same source runs on 2024 through 2026+.

def eid_value(element_id):
    """The integer value of an ElementId, on any Revit version."""
    if element_id is None:
        return None
    try:
        return element_id.Value          # Revit 2024+
    except AttributeError:
        return element_id.IntegerValue   # Revit 2023 and earlier


def element_id(value):
    """Builds an ElementId from an integer, on any Revit version."""
    if value is None:
        return None
    try:
        from System import Int64
        return ElementId(Int64(value))   # Revit 2024+ (Int64 overload)
    except Exception:
        return ElementId(int(value))     # Revit 2023 and earlier


# Parameter names that actually CONTROL a panel's schedule capacity and
# are safe to WRITE to (used by bump_panel_capacity to grow a panel's
# capacity). Kept intentionally narrow and separate from
# NOMINAL_SLOT_PARAM_NAMES below - "Slots" or "Number of Poles/Slots"
# on some families may be an unrelated/read-only custom parameter that
# happens to exist without actually controlling the schedule's real
# row count, so this list must never be widened casually; only add a
# name here if it's confirmed to be the genuine, official,
# capacity-controlling parameter for that family type.
CAPACITY_PARAM_NAMES = [
    "Max Number of Single Pole Breakers",  # panelboards, current Revit
    "Max #1 Pole Breakers",                # panelboards, older Revit
    "Max Number of Circuits",              # switchboards, current Revit
]

# Broader list used ONLY for the initial, read-only nominal slot-count
# GUESS in build_panel_info (never written to) - includes
# CAPACITY_PARAM_NAMES plus a couple of common generic names some
# panel families use instead. Safe to be broad here because this is
# purely informational: it's immediately corrected against the
# schedule's real slot count wherever a schedule already exists (or
# as soon as one is created), so a wrong guess here is never load-bearing.
NOMINAL_SLOT_PARAM_NAMES = CAPACITY_PARAM_NAMES + [
    "Slots",
    "Number of Poles/Slots",
]


# ---------------------------------------------------------------------------
# Panel discovery
# ---------------------------------------------------------------------------

def get_all_electrical_panels(doc):
    """
    Returns a list of Revit FamilyInstance elements that are electrical
    panels (distribution boards), i.e. elements in the
    OST_ElectricalEquipment category that have an associated
    "Panel Schedule" capability (their MEPModel exposes
    AssignedElectricalSystems / they can host a PanelScheduleView).
    """
    collector = FilteredElementCollector(doc) \
        .OfCategory(BuiltInCategory.OST_ElectricalEquipment) \
        .WhereElementIsNotElementType()

    panels = []
    for el in collector:
        try:
            mep_model = el.MEPModel
            if mep_model is None:
                continue
            # Panels expose a PanelScheduleView-eligible "Electrical
            # Data". The presence of the "Distribution System"
            # parameter is the discriminator between a generic
            # equipment item and an actual panel.
            has_panel_capability = el.LookupParameter("Distribution System") is not None
            if has_panel_capability:
                panels.append(el)
        except Exception:
            continue
    return panels


def selected_panels_from_selection(doc, uidoc):
    """Returns panel FamilyInstance elements from the current selection."""
    sel_ids = uidoc.Selection.GetElementIds()
    result = []
    for eid in sel_ids:
        el = doc.GetElement(eid)
        if el is not None and el.Category is not None \
                and eid_value(el.Category.Id) == int(BuiltInCategory.OST_ElectricalEquipment):
            result.append(el)
    return result


# ---------------------------------------------------------------------------
# Panel + circuit -> PanelInfo / CircuitInfo (read)
# ---------------------------------------------------------------------------

def _get_param_string(element, bip, default=""):
    try:
        p = element.get_Parameter(bip)
        if p is None:
            return default
        if p.StorageType == StorageType.String:
            return p.AsString() or default
        return p.AsValueString() or default
    except Exception:
        return default


def _get_param_double(element, bip, default=0.0):
    try:
        p = element.get_Parameter(bip)
        if p is None:
            return default
        return p.AsDouble()
    except Exception:
        return default


def get_schedule_body_row_count(panel_schedule_view, doc=None):
    """
    Returns the ACTUAL number of rows in the panel schedule's Body
    section, as reported by Revit itself (read-only call, safe to use
    outside a Transaction), rather than assuming it matches the panel
    family's "Slots" / "Number of Poles" parameter. See the module
    docstring's "SECOND CORRECTION" note for why this matters.

    FOURTH CORRECTION (severe - a live run crashed Revit itself, not
    just raised a catchable Python exception): an earlier revision of
    this function called `doc.Regenerate()` as a retry when
    `GetTableData()` returned None, on the theory that this was always
    safe outside a Transaction. On at least one real model this
    triggered "Failed to regenerate" and took the whole Revit process
    down - a genuine document-state issue that `Regenerate()` cannot
    safely resolve is NOT something a try/except can protect against,
    since the crash isn't a normal .NET exception at that point. The
    `doc` parameter is kept (for call-site compatibility) but is now
    intentionally UNUSED - this function no longer calls Regenerate()
    under any circumstances. If GetTableData() returns None or throws,
    this simply returns 0 and logs a warning; fixing the underlying
    document state (a manual Regenerate/reload inside Revit's own UI,
    where the user can see and respond to any warning dialogs) is left
    to the user rather than attempted automatically.
    """
    try:
        table_data = panel_schedule_view.GetTableData()
        section = table_data.GetSectionData(SectionType.Body)
        return section.NumberOfRows
    except Exception:
        logger.warning(
            "Could not read actual body row count for schedule '%s'.",
            panel_schedule_view.Name, exc_info=True
        )
        return 0


def build_panel_info(doc, panel_element):
    """
    Reads a panel FamilyInstance + its assigned ElectricalSystems into
    a PanelInfo/CircuitInfo tree that the rest of the app operates on.
    """
    mep_model = panel_element.MEPModel
    # THIRTIETH CORRECTION: `FamilyInstance.Name` returns the family
    # TYPE name ("225A 42-circuit panelboard"), not the panel's own
    # "Panel Name" ("LP-2", "ACP-01"). Every downstream consumer of
    # this value assumes the latter: `match_panel_type` matches the
    # prefix against the type-config table to choose the spare breaker
    # rating and pole count, and every log line and report row
    # identifies the panel by it. Matching against a type name meant
    # the prefix lookup essentially never hit, so every panel silently
    # fell back to the DEFAULT 20 A / 1-pole spare regardless of what
    # the Type Config table said. Falls back to the old behaviour only
    # if the parameter is genuinely absent.
    name = _get_param_string(
        panel_element, BuiltInParameter.RBS_ELEC_PANEL_NAME
    ) or panel_element.Name

    dist_system_param = panel_element.LookupParameter("Distribution System")
    phase_config = THREE_PHASE
    try:
        if dist_system_param is not None:
            val = dist_system_param.AsValueString() or ""
            if "1" in val or "Single" in val:
                phase_config = SINGLE_PHASE
    except Exception:
        pass

    # SIXTH CORRECTION: this used to only try "Slots" and "Number of
    # Poles/Slots" before defaulting straight to a hardcoded 42 - a
    # live run showed EVERY panel in the project (12/18/24/60-way
    # alike) logging "nominal slot count (42)", meaning neither of
    # those two names actually existed on any of these panel
    # families, so every single one silently fell through to the
    # hardcoded default. The correct parameter name was one of the
    # ones already used by `bump_panel_capacity` for the capacity-bump
    # feature (e.g. "Max Number of Single Pole Breakers") but was
    # never tried here. This now tries `NOMINAL_SLOT_PARAM_NAMES`,
    # which is `CAPACITY_PARAM_NAMES` plus a couple of generic
    # fallback names - the two lists share that common base so a
    # correct name discovered for one purpose is automatically tried
    # for the other too, while still keeping write access
    # (bump_panel_capacity) restricted to the narrower,
    # confirmed-safe-to-write `CAPACITY_PARAM_NAMES` only. Note: even
    # before this fix, a WRONG nominal guess here was harmless for
    # panels with an EXISTING schedule (the reconciliation a few lines
    # below already corrects it against the schedule's real slot
    # count) and is corrected again at Execute time for newly-created
    # schedules - but it did make the Step 1 panel picker's "Slots"
    # column show a wrong value before any schedule existed to correct
    # it against, which is what this fixes.
    # NINTH CORRECTION: this loop used to stop at the first parameter
    # name that merely EXISTED, even if its value was 0 - which
    # happens when a panelboard-specific parameter (e.g. "Max Number
    # of Single Pole Breakers") is also present but unused/zero on a
    # switchboard-style family that actually stores its real capacity
    # under a different name later in the list (e.g. "Max Number of
    # Circuits"). A live run showed 60-way switchboard panels logging
    # "nominal slot count (0)" for exactly this reason. Fixed to only
    # accept and stop on a STRICTLY POSITIVE value, continuing to try
    # the remaining candidate names otherwise, and falling back to the
    # original 42 placeholder (not 0) if none of them ever produce a
    # positive number.
    num_slots = 42
    for pname in NOMINAL_SLOT_PARAM_NAMES:
        slots_param = panel_element.LookupParameter(pname)
        if slots_param is None:
            continue
        try:
            candidate = int(slots_param.AsInteger())
        except Exception:
            continue
        if candidate > 0:
            num_slots = candidate
            break

    panel_info = PanelInfo(
        element_id=eid_value(panel_element.Id),
        name=name,
        phase_config=phase_config,
        num_slots=num_slots,
    )

    circuits = []
    # FORTY-NINTH CORRECTION: `MEPModel.AssignedElectricalSystems` no
    # longer exists - the property was replaced by the METHOD
    # `GetAssignedElectricalSystems()` (Revit 2021+). The `getattr(...,
    # None) or []` here therefore returned an empty list for EVERY panel
    # (the log shows "AssignedElectricalSystems reported 0 circuit(s)" for
    # all 476 panels of a project). Panels with a schedule were rescued
    # by the schedule scan below; a panel WITHOUT a schedule had no
    # circuits at all in Preview. The method is tried first, the old
    # property only as a fallback for very old Revit versions.
    assigned_systems = []
    try:
        found = mep_model.GetAssignedElectricalSystems()
        assigned_systems = list(found) if found is not None else []
    except Exception:
        try:
            assigned_systems = list(
                getattr(mep_model, "AssignedElectricalSystems", None) or [])
        except Exception:
            assigned_systems = []
    for sys_el in assigned_systems:
        try:
            circuits.append(_build_circuit_info(sys_el))
        except Exception:
            logger.exception("Failed to read circuit %s", sys_el.Id)

    panel_info.circuits = circuits

    # Existing panel schedule view, if any.
    psv_id = _find_panel_schedule_view_id(doc, panel_element)
    panel_info.panel_schedule_view_id = psv_id
    # TWENTY-SIXTH CORRECTION: remember that this schedule is one the
    # project already had, as opposed to one this tool is about to
    # create. `BalancingSettings.preserve_existing_schedule` keys off
    # this to leave issued schedules physically untouched.
    panel_info.schedule_pre_existed = psv_id is not None

    if psv_id is not None:
        # Authoritative correction: use the schedule's REAL slot count
        # (GetTableData().NumberOfSlots - see module docstring, THIRD
        # CORRECTION) instead of the family "Slots" parameter, since
        # these are not guaranteed to match. This is a read-only call
        # and safe to run outside a Transaction, so we can do it
        # during Step 1 discovery / Step 4 preview, not just at
        # Execute time.
        try:
            existing_view = doc.GetElement(element_id(psv_id))
            actual_slots = get_total_slots(existing_view, doc=doc)
            if actual_slots and actual_slots != panel_info.num_slots:
                logger.warning(
                    "Panel '%s': nominal slot count (%s) does not match "
                    "existing schedule slot count (%s). Using %s as "
                    "the authoritative capacity.",
                    name, panel_info.num_slots, actual_slots, actual_slots
                )
                panel_info.num_slots = actual_slots

            # TWELFTH CORRECTION + FOURTEENTH CORRECTION: populate each
            # circuit's REAL current slot, and recover any circuit the
            # schedule knows about that AssignedElectricalSystems
            # missed - see reconcile_circuits_with_schedule's docstring
            # for the full account (a live run showed balancing seeing
            # ZERO circuits for a panel with real, substantial loads).
            slot_map = get_circuit_slot_map(existing_view, panel_info.num_slots)
            reconcile_circuits_with_schedule(doc, panel_info, slot_map, name,
                                             panel_schedule_view=existing_view)
        except Exception:
            logger.exception(
                "Failed to reconcile slot count against existing schedule "
                "for panel '%s'; keeping nominal value %s.",
                name, panel_info.num_slots
            )

    return panel_info


def get_circuit_slot_map(panel_schedule_view, total_slots):
    """
    TWELFTH CORRECTION: `CircuitInfo.slot` was never actually being
    populated from real Revit data anywhere in this codebase - it
    stayed at its default of `None` for every circuit read from Revit,
    all the way through balancing and arrangement. This meant
    `swap_schedule_slots` never had a valid "from" position to move a
    circuit FROM, and `arrangement_service`'s locked-circuit handling
    (`if c.locked and c.slot:`) was silently skipped every time. This
    function scans every slot in the schedule, resolves it to a
    (row, col) cell, reads the circuit occupying that cell via
    `GetCircuitByCell`, and matches it back to a real ElectricalSystem
    by ElementId - returning {element_id: slot_number} so callers can
    populate `CircuitInfo.slot` with the circuit's ACTUAL current
    physical position before planning or applying any move. Read-only;
    safe to call outside a Transaction.
    """
    slot_map = {}
    for slot in range(1, total_slots + 1):
        row, col = _resolve_cell(panel_schedule_view, slot)
        if row is None:
            continue
        try:
            circuit = panel_schedule_view.GetCircuitByCell(row, col)
        except Exception:
            continue
        if circuit is None:
            continue
        try:
            circuit_eid = eid_value(circuit.Id)
        except Exception:
            continue
        # Only record the FIRST (lowest) slot a multi-pole circuit's
        # Id appears at, since that's its starting/reference slot for
        # move purposes.
        if circuit_eid not in slot_map:
            slot_map[circuit_eid] = slot
    return slot_map


def read_slot_cells(panel_schedule_view, total_slots):
    """{slot: (row, col)} for every slot the schedule really has. Read-only."""
    cells = {}
    for slot in range(1, int(total_slots or 0) + 1):
        row, col = _resolve_cell(panel_schedule_view, slot)
        if row is not None:
            cells[slot] = (int(row), int(col))
    return cells


def read_layout_into(panel_info, panel_schedule_view):
    """
    FORTY-FIFTH CORRECTION. Reads how THIS schedule maps slots to rows /
    columns and stores the resulting `models.SlotLayout` on the panel, so
    phases and multi-pole slot runs come from the real template instead
    of an assumed two-column layout. The real leg of every loaded
    single-pole circuit (Revit's own per-phase load) is used to confirm
    which leg the first row is on and to measure how far the layout can
    be trusted. Read-only; safe outside a Transaction.
    """
    from panelmgr.models import build_layout
    labels = panel_info.legs()
    try:
        cells = read_slot_cells(panel_schedule_view, panel_info.num_slots)
    except Exception:
        logger.warning("Panel '%s': could not read the schedule's cell layout.",
                       panel_info.name, exc_info=True)
        cells = {}
    evidence = []
    for circuit in panel_info.circuits:
        if circuit.is_placeholder() or circuit.poles != 1 or circuit.slot is None:
            continue
        legs = circuit.actual_legs(labels)
        if len(legs) == 1:
            evidence.append((circuit.slot, legs[0]))
    panel_info.layout = build_layout(cells, labels, evidence) if cells else None
    return panel_info.layout


def read_actual_phase_loads(doc, panel_info, labels):
    """
    {leg: VA} summed from Revit's OWN per-phase apparent load of every
    real circuit on the panel, read fresh from the document - or None if
    nothing could be read. Called after the update has committed, this
    is the true result of the balancing, independent of anything this
    tool assumed about slots and phases.
    """
    totals = dict((p, 0.0) for p in labels)
    found = False
    for circuit in panel_info.circuits:
        if circuit.is_placeholder():
            continue
        try:
            el = doc.GetElement(element_id(circuit.element_id))
            phase_va = _read_phase_va(el)
        except Exception:
            phase_va = None
        if not phase_va:
            continue
        circuit.phase_va = phase_va
        for index, va in enumerate(phase_va):
            if index < len(labels) and va:
                totals[labels[index]] += va
                found = True
    return totals if found else None


def _read_phase_va(electrical_system):
    """(A, B, C) apparent load in VA of one circuit, or None."""
    if electrical_system is None:
        return None
    values = []
    for name in ("RBS_ELEC_APPARENT_LOAD_PHASEA", "RBS_ELEC_APPARENT_LOAD_PHASEB",
                 "RBS_ELEC_APPARENT_LOAD_PHASEC"):
        bip = getattr(BuiltInParameter, name, None)
        if bip is None:
            return None
        try:
            param = electrical_system.get_Parameter(bip)
            if param is None or not param.HasValue:
                return None
            values.append(_va_from_internal_units(param.AsDouble()))
        except Exception:
            return None
    return tuple(values)


def reconcile_circuits_with_schedule(doc, panel_info, slot_map, panel_name,
                                      phase_labels=None, panel_schedule_view=None):
    """
    FOURTEENTH CORRECTION: a live run showed balancing seeing ZERO
    circuits (Total Connected Load = 0) for a panel that visibly had
    real, substantial loads on its printed schedule - while
    spare/space's SEPARATE schedule-based count
    (`count_connected_ways`, using the same `GetCircuitByCell` scan as
    `get_circuit_slot_map`) correctly found real content. This means
    `panel_element.MEPModel.AssignedElectricalSystems` (the sole
    source `build_panel_info` originally used for `panel_info
    .circuits`) can return empty/incomplete for a panel whose schedule
    clearly shows circuits - the schedule-based scan is the more
    reliable source here.

    This function does two things:
      1. Populates each already-known circuit's REAL current slot from
         `slot_map` (see `get_circuit_slot_map`'s docstring - the
         TWELFTH CORRECTION - for why this matters on its own).
      2. For any circuit id `slot_map` found that ISN'T already in
         `panel_info.circuits`, attempts to read it directly as a real
         circuit and appends it - so balancing can actually see and
         move it. If a given id can't be read as a real circuit (an
         exception - logged, not swallowed), that likely means the row
         is a manually-entered schedule-only value with no backing
         ElectricalSystem element, which genuinely cannot be
         balanced/moved via the API (there is no real circuit object
         to move) - this is surfaced clearly rather than silently
         ignored, since it changes what's actually achievable for that
         panel.
    """
    known_ids = set(c.element_id for c in panel_info.circuits)
    for circuit in panel_info.circuits:
        if circuit.element_id in slot_map:
            circuit.slot = slot_map[circuit.element_id]

    extra_ids = set(slot_map.keys()) - known_ids
    if extra_ids:
        logger.warning(
            "Panel '%s': AssignedElectricalSystems reported %d circuit(s), "
            "but the schedule itself shows %d slot(s) with real content - "
            "%d circuit(s) known to the schedule were missing from "
            "circuit-level discovery. Attempting to read them directly so "
            "balancing can see them.",
            panel_name, len(panel_info.circuits), len(slot_map), len(extra_ids)
        )
        for extra_id in extra_ids:
            try:
                extra_el = doc.GetElement(element_id(extra_id))
                if extra_el is None:
                    continue
                extra_circuit = _build_circuit_info(extra_el)
                extra_circuit.slot = slot_map[extra_id]
                panel_info.circuits.append(extra_circuit)
            except Exception:
                logger.exception(
                    "Panel '%s': found circuit id %s via the schedule scan, "
                    "but could not read it as a real circuit - it may be a "
                    "manually-entered schedule-only row with no backing "
                    "ElectricalSystem element, which cannot be "
                    "balanced/moved via the API.", panel_name, extra_id
                )

    # FIFTEENTH CORRECTION: `CircuitInfo.phases` was never populated
    # from real Revit data either (`_build_circuit_info` never sets
    # it, so every circuit started with an empty tuple) - a live run
    # showed "Imbalance before" reporting a false 0% for a panel that
    # clearly needed rebalancing (26 circuits ended up moved), because
    # `compute_phase_loads` sums nothing for a circuit with no phases,
    # making every phase's "before" total look like 0 regardless of
    # the panel's real starting state. Every circuit with a known slot
    # (from `get_circuit_slot_map` above) now gets its phases derived
    # from that slot using the same step-of-2 (same-column)
    # `circuit_slot_range` convention `arrangement_service` itself
    # plans against (see its EIGHTEENTH CORRECTION) - so the "before"
    # snapshot reflects the panel's ACTUAL starting phase distribution.
    # THIRTY-NINTH CORRECTION: this derived phases from the hardcoded
    # three-leg R-Y-B cycle in `phase_for_slot` for EVERY panel,
    # including split-phase (120/240 V) boards, which
    # `balancing_service.phase_set_for_panel` models as two legs named
    # L1/L2. The result on a single-phase panel was a circuit set
    # carrying phases "R"/"Y"/"B" while every calculation that
    # followed expected "L1"/"L2":
    #   - `_fixed_multipole_loads` only keys L1/L2, so multi-pole load
    #     on an "R" leg was dropped from the running totals entirely;
    #   - `compute_phase_loads` (since the TWENTY-EIGHTH CORRECTION
    #     stopped it raising KeyError) instead ACCUMULATED the stray
    #     legs, returning up to five legs - L1, L2, R, Y and B - two of
    #     which were always 0.0, so `compute_imbalance_percentage`
    #     divided by a five-way average and reported a fictional
    #     imbalance.
    # The leg names now follow the panel's own phase configuration.
    # FORTY-FIFTH CORRECTION: the leg of a slot now comes from the
    # schedule's REAL row / column layout (read here, now that every
    # circuit and its slot is known), not from the two-column formula
    # this used before. See models.SlotLayout.
    if panel_schedule_view is not None:
        read_layout_into(panel_info, panel_schedule_view)
    layout = panel_info.get_layout()

    mismatched = 0
    for circuit in panel_info.circuits:
        if circuit.slot is not None:
            circuit.phases = tuple(
                leg for leg in (layout.phase(s)
                                for s in layout.run(circuit.slot, circuit.poles))
                if leg is not None
            )
            actual = circuit.actual_legs(layout.labels)
            if actual and not circuit.is_placeholder() \
                    and set(actual) != set(circuit.phases):
                mismatched += 1
    if mismatched:
        logger.warning(
            "Panel '%s': %d circuit(s) sit on a slot whose phase (from the "
            "schedule layout) differs from the phase Revit reports for the "
            "circuit's load.", panel_name, mismatched
        )


def _va_from_internal_units(raw_value):
    """
    TWENTY-SECOND CORRECTION (critical, present since this tool's
    first version): `BuiltInParameter.RBS_ELEC_APPARENT_LOAD` (and the
    per-phase variants) return Revit's RAW INTERNAL UNITS value, NOT
    real-world VA - confirmed empirically against a live discrepancy
    (a reported "Total Connected Load" of 449716.2 against Revit's own
    printed schedule showing 41780 VA for the same panel - a ratio of
    10.7639..., matching EXACTLY 1 / 0.3048^2 to 6 significant
    figures) and independently confirmed by multiple Autodesk forum
    threads describing this same parameter returning a value "always
    10.7639... times larger than the correct value," with the
    documented fix being to multiply the raw value by 0.3048^2 (Revit
    apparently represents this unit-less-in-SI-terms quantity
    internally using a square-feet-based placeholder unit basis, for
    reasons rooted in its internal unit system architecture rather
    than any real physical square-footage meaning). This conversion
    has been missing from EVERY load figure this tool has ever
    computed or reported. Critically, this did NOT affect the
    CORRECTNESS of the balancing/imbalance-percentage math itself,
    since every circuit's load was uniformly inflated by the same
    constant factor, leaving relative comparisons (which phase is
    heavier, by how much percentage) unaffected - only the ABSOLUTE
    "Total Load" / per-phase VA figures shown in reports were wrong.
    """
    return raw_value * (0.3048 ** 2)


def _va_to_internal_units(va_value):
    """
    Inverse of `_va_from_internal_units` - use whenever WRITING a load
    value back into a Revit load parameter (e.g.
    `PanelScheduleView.SetSpareLoadValue`), which expects the same raw
    internal-unit basis the getters return.

    NOTE for future maintenance: the 0.3048**2 constant here and in
    `_va_from_internal_units` is empirically derived (see that
    function's TWENTY-SECOND CORRECTION). The officially supported
    equivalent is
    `UnitUtils.ConvertToInternalUnits(va, UnitTypeId.VoltAmperes)` /
    `ConvertFromInternalUnits`, which is version-safe and should
    replace both of these once the project's minimum supported Revit
    version is confirmed to be 2021+ (UnitTypeId was introduced in
    2021; DisplayUnitType before that). The constant is kept for now
    so this change does not also change every existing reported
    figure.
    """
    return va_value / (0.3048 ** 2)


# The four load parameters `SetSpareLoadValue` / `GetSpareLoadValue`
# accept, per the Revit API docs. Keyed by our own phase labels so the
# balancing service's {phase: va} allocation can be written directly.
SPARE_LOAD_PARAM_BY_PHASE = {
    "R": BuiltInParameter.RBS_ELEC_APPARENT_LOAD_PHASEA,
    "Y": BuiltInParameter.RBS_ELEC_APPARENT_LOAD_PHASEB,
    "B": BuiltInParameter.RBS_ELEC_APPARENT_LOAD_PHASEC,
    "L1": BuiltInParameter.RBS_ELEC_APPARENT_LOAD_PHASEA,
    "L2": BuiltInParameter.RBS_ELEC_APPARENT_LOAD_PHASEB,
}


def set_spare_slot_loads(panel_schedule_view, row, col, phase_va_map,
                         panel_name="", log_fn=None):
    """
    Multi-leg form of `set_spare_slot_load`: `phase_va_map` is
    {phase: va} for every leg this one spare occupies (a 3-pole spare
    touches all three). The total RBS_ELEC_APPARENT_LOAD is written as
    the sum, so the spare's own schedule row and the per-leg summary
    agree with each other.
    """
    log_fn = log_fn or (lambda msg: None)
    ok = True
    total_va = 0.0
    for phase, va in phase_va_map.items():
        bip = SPARE_LOAD_PARAM_BY_PHASE.get(phase)
        if bip is None:
            log_fn("Panel '{}': no load parameter maps to phase '{}' - "
                   "spare at row {} col {} left at 0 VA on that leg.".format(
                       panel_name, phase, row, col))
            ok = False
            continue
        va = max(0.0, float(va))
        total_va += va
        try:
            panel_schedule_view.SetSpareLoadValue(
                row, col, ElementId(bip), _va_to_internal_units(va)
            )
        except Exception as ex:
            log_fn("Panel '{}': could not set spare load on phase {} at "
                   "row {} col {} ({}).".format(panel_name, phase, row, col, ex))
            logger.warning("SetSpareLoadValue failed at (%s,%s) phase %s on '%s'",
                           row, col, phase, panel_schedule_view.Name, exc_info=True)
            ok = False
    try:
        panel_schedule_view.SetSpareLoadValue(
            row, col, ElementId(BuiltInParameter.RBS_ELEC_APPARENT_LOAD),
            _va_to_internal_units(total_va)
        )
    except Exception:
        logger.debug("Could not set total apparent load on spare (%s,%s)",
                     row, col, exc_info=True)
    return ok


def set_spare_slot_load(panel_schedule_view, row, col, phase, va_value,
                        panel_name="", log_fn=None):
    """
    TWENTY-SEVENTH CORRECTION. Writes an apparent load onto a SPARE
    slot via `PanelScheduleView.SetSpareLoadValue(row, col,
    ElementId(loadParam), value)`.

    Until now this extension placed spares with a pole count and a
    breaker rating but never gave them a load, so every spare it
    created read as 0 VA. That meant the panel's own loads-summary
    (and every phase total downstream of it) did not reflect the new
    spare/space layout at all - the schedule declared spare ways
    without declaring the capacity they reserve, which is exactly the
    figure a reviewer checks.

    Writes BOTH the specific phase parameter (so the leg totals move)
    and RBS_ELEC_APPARENT_LOAD (so the spare's own row shows its
    load). Must be called from inside an open Transaction. Returns
    True on success.

    A Space is NOT a valid target - it has no breaker, so it reserves
    no capacity and Revit raises ArgumentsInconsistentException for a
    non-spare cell. Callers must only pass cells they placed as spares.
    """
    log_fn = log_fn or (lambda msg: None)
    bip = SPARE_LOAD_PARAM_BY_PHASE.get(phase)
    if bip is None:
        log_fn("Panel '{}': no load parameter maps to phase '{}' - spare "
               "at row {} col {} left at 0 VA.".format(
                   panel_name, phase, row, col))
        return False
    try:
        internal = _va_to_internal_units(max(0.0, float(va_value)))
        panel_schedule_view.SetSpareLoadValue(
            row, col, ElementId(bip), internal
        )
        panel_schedule_view.SetSpareLoadValue(
            row, col, ElementId(BuiltInParameter.RBS_ELEC_APPARENT_LOAD),
            internal
        )
        return True
    except Exception as ex:
        log_fn("Panel '{}': could not set spare load at row {} col {} "
               "({}).".format(panel_name, row, col, ex))
        logger.warning("SetSpareLoadValue failed at (%s,%s) on '%s'",
                       row, col, panel_schedule_view.Name, exc_info=True)
        return False


def phase_for_row(row, base_row, phase_labels):
    """
    Which phase leg a schedule Body ROW sits on, under the same
    R-Y-B-repeat-every-3-rows model `_find_free_run` already uses for
    phase-aligned multi-pole placement. Kept here (rather than
    re-deriving it at each call site) so the spare-load writer and the
    placement search can never drift onto different conventions - the
    exact class of bug the EIGHTEENTH/NINETEENTH corrections were
    about.
    """
    if not phase_labels:
        return None
    return phase_labels[((row - base_row) % len(phase_labels))]


def _build_circuit_info(electrical_system):
    circuit_number = _get_param_string(
        electrical_system, BuiltInParameter.RBS_ELEC_CIRCUIT_NUMBER
    )
    description = _get_param_string(
        electrical_system, BuiltInParameter.RBS_ELEC_CIRCUIT_NAME
    )
    load_va = _va_from_internal_units(_get_param_double(
        electrical_system, BuiltInParameter.RBS_ELEC_APPARENT_LOAD
    ))
    try:
        poles = int(electrical_system.PolesNumber)
    except Exception:
        poles = 1

    load_class = ""
    try:
        lc_param = electrical_system.LookupParameter("Load Classification")
        if lc_param is not None:
            load_class = lc_param.AsValueString() or ""
    except Exception:
        pass

    # TWENTIETH CORRECTION: approximate Watts (true/real power) from
    # VA (apparent power) via Power Factor, when a Power Factor
    # parameter is readable on the circuit. Handles both common
    # storage conventions defensively: a ratio (0.0-1.0) or a
    # percentage-formatted value (0-100) - if the raw value is > 1.0
    # it's treated as a percentage. Falls back to PF=1.0 (true_load_w
    # == apparent_load_va) when no Power Factor parameter exists or
    # its value can't be read, rather than fabricating a number - this
    # is a per-circuit approximation, NOT a replication of Revit's own
    # project-wide category/demand-factor Watts calculations (see
    # models.CircuitInfo's TWENTIETH CORRECTION docstring for the full
    # caveat).
    true_load_w = load_va
    try:
        pf_param = electrical_system.LookupParameter("Power Factor")
        if pf_param is not None:
            pf_raw = pf_param.AsDouble()
            if pf_raw > 0:
                power_factor = pf_raw / 100.0 if pf_raw > 1.0 else pf_raw
                true_load_w = load_va * power_factor
    except Exception:
        pass

    # TWENTY-NINTH CORRECTION: `is_spare` / `is_space` were never
    # populated from Revit anywhere - both stayed False for every
    # circuit this function produced. That mattered because Spare and
    # Space rows in a panel schedule ARE returned by
    # `GetCircuitByCell` as circuit-like objects (which is precisely
    # why `count_connected_ways` has to filter them out by LoadName),
    # and `reconcile_circuits_with_schedule` appends anything the
    # schedule scan finds into `panel_info.circuits`. The net effect
    # was that existing spares and spaces were treated as REAL
    # circuits: they inflated the "connected ways" count that drives
    # the Ways-table bracket lookup, they were fed into the balancing
    # pool as movable load, and arrangement would happily plan
    # MoveSlotTo calls for them. Detected here by LoadName, the same
    # discriminator `count_connected_ways` already relies on.
    load_name = ""
    try:
        load_name = (electrical_system.LoadName or "").strip().upper()
    except Exception:
        load_name = (description or "").strip().upper()

    return CircuitInfo(
        element_id=eid_value(electrical_system.Id),
        circuit_number=circuit_number,
        description=description,
        poles=poles,
        apparent_load_va=load_va,
        true_load_w=true_load_w,
        load_classification=load_class,
        is_spare=(load_name == "SPARE"),
        is_space=(load_name == "SPACE"),
        phase_va=_read_phase_va(electrical_system),
    )


def get_panel_schedule_templates(doc):
    """
    Returns a list of (name, element_id_int) for every
    Autodesk.Revit.DB.Electrical.PanelScheduleTemplate in the project
    (Electrical Settings -> Panel Schedule Templates). These are the
    firm/company-specific templates users configure per distribution
    system in Revit's UI; passing one of their ElementIds into
    `create_or_get_panel_schedule_view` controls exactly which
    template a newly created schedule uses, instead of leaving it to
    whatever Revit calls "default" for that panel's distribution
    system.
    """
    templates = []
    try:
        collector = FilteredElementCollector(doc).OfClass(PanelScheduleTemplate)
        for t in collector:
            try:
                templates.append((t.Name, eid_value(t.Id)))
            except Exception:
                continue
    except Exception:
        logger.exception("Failed to enumerate PanelScheduleTemplate elements")
    return templates


def _find_panel_schedule_view_id(doc, panel_element):
    try:
        collector = FilteredElementCollector(doc).OfClass(PanelScheduleView)
        for view in collector:
            try:
                if view.GetPanel() == panel_element.Id:
                    return eid_value(view.Id)
            except Exception:
                continue
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Panel schedule view creation (write - must run inside a Transaction)
# ---------------------------------------------------------------------------

def create_or_get_panel_schedule_view(doc, panel_element, panel_info, template_id=None):
    """
    Creates a PanelScheduleView for the given panel if one does not
    already exist. Must be called from inside an open Transaction.
    Returns the PanelScheduleView.

    Args:
        template_id (int|None): integer id (ElementId.Value) of a specific
            Autodesk.Revit.DB.Electrical.PanelScheduleTemplate to use
            (see `get_panel_schedule_templates`). If None, falls back
            to the 2-argument overload, which uses whichever template
            is configured as the DEFAULT for that panel's distribution
            system under Electrical Settings -> Panel Schedule
            Templates in the Revit UI - this is a project-level
            setting, not something this tool controls, so results can
            vary between projects if no explicit template is chosen.
    """
    existing_id = _find_panel_schedule_view_id(doc, panel_element)
    if existing_id is not None:
        return doc.GetElement(element_id(existing_id))

    # PanelScheduleView.CreateInstanceView has two static overloads:
    #   CreateInstanceView(Document, ElementId panelId)
    #       -> uses the project's default panel schedule template for
    #          that panel's distribution system
    #   CreateInstanceView(Document, ElementId panelId, ElementId templateId)
    #       -> uses the specific PanelScheduleTemplate given
    # BOTH overloads can return None (not raise) if Revit can't build a
    # schedule for this panel - e.g. the chosen/default template's
    # phase config or voltage doesn't match this panel's distribution
    # system, or no default template is assigned at all for that
    # distribution system in Electrical Settings. A live traceback
    # ("'NoneType' object has no attribute 'Name'") confirmed this
    # happens silently rather than throwing, so both branches are
    # checked explicitly below instead of assuming success.
    view = None
    if template_id is not None:
        view = PanelScheduleView.CreateInstanceView(
            doc, panel_element.Id, element_id(template_id)
        )
        if view is None:
            logger.warning(
                "Selected template (id=%s) could not build a schedule for "
                "panel '%s' (likely a phase/voltage mismatch with this "
                "panel's distribution system) - falling back to the "
                "project default template.",
                template_id, panel_info.name
            )
            view = PanelScheduleView.CreateInstanceView(doc, panel_element.Id)

    if view is None and template_id is None:
        view = PanelScheduleView.CreateInstanceView(doc, panel_element.Id)

    if view is None:
        raise RuntimeError(
            "Revit could not create a panel schedule for '{}'. This "
            "usually means no panel schedule template is assigned as "
            "the default for this panel's distribution system "
            "(Electrical Settings -> Panel Schedule Templates), and no "
            "compatible template was selected in Step 1 either. Assign "
            "a default template for this panel's distribution system, "
            "or pick one explicitly in Step 1, then re-run.".format(
                panel_info.name)
        )

    # SEVENTH CORRECTION: a live run showed that a FRESHLY CREATED
    # PanelScheduleView's GetTableData() returns None if queried
    # immediately, without ever regenerating.
    #
    # EIGHTH/NINTH CORRECTIONS (superseded): an explicit `doc.Regenerate()`
    # call was added here to fix that, wrapped defensively after it hit
    # "Failed to regenerate" on one panel. It then went on to hit the
    # EXACT SAME error on every single subsequent new-schedule-creation
    # attempt across multiple different panel types (100% reproducibility,
    # confirmed directly) - conclusive evidence the explicit mid-transaction
    # Regenerate() call itself is the unreliable part in this project, not
    # (necessarily) the individual panels. This call has been REMOVED.
    # `create_or_get_panel_schedule_view` is now called in its OWN
    # transaction (see schedule_service.execute_batch) that commits
    # immediately after creation - relying on Transaction.Commit()'s
    # IMPLICIT regeneration (a different, more mature Revit API code path)
    # instead of an explicit mid-transaction call. This is an experiment,
    # not a guaranteed fix - if the underlying issue is that Revit
    # genuinely cannot create any new panel schedule in this document at
    # all right now, no code-level change can work around that; a manual
    # test creating a schedule via Revit's own native UI (Systems tab ->
    # Panel Schedules -> New) is the definitive way to tell the difference.

    logger.info("Created PanelScheduleView '%s' (id=%s)",
                view.Name, view.Id)
    panel_info.panel_schedule_view_id = eid_value(view.Id)
    return view


# ---------------------------------------------------------------------------
# Circuit number / phase writes
# ---------------------------------------------------------------------------

def try_set_circuit_number(electrical_system, new_number):
    """
    Attempts to write a new circuit number LABEL only.

    WARNING (TWELFTH CORRECTION): this does NOT reliably move a
    circuit to a different physical phase/slot, even when the write
    "succeeds" (no exception, parameter not read-only). A live run
    showed a panel's connected loads remaining exactly as imbalanced
    after "balancing" as before, despite this call reporting success -
    strong evidence that RBS_ELEC_CIRCUIT_NUMBER is a DISPLAYED LABEL
    that can be edited independently of which physical slot (and
    therefore which phase) a circuit actually occupies; a circuit's
    real phase is determined by its physical slot position, not by
    this text value. `schedule_service._apply_slot_plan` no longer
    calls this function for that reason - it uses `swap_schedule_slots`
    (MoveSlotTo) exclusively, which performs genuine physical
    repositioning. This function is kept only as a plain,
    lower-level utility for the rare case where a firm's template
    happens to use fully-manual, disconnected-from-slot numbering and
    a caller genuinely wants to relabel a circuit without moving it -
    it must not be used as a substitute for an actual slot move.
    """
    try:
        p = electrical_system.get_Parameter(
            BuiltInParameter.RBS_ELEC_CIRCUIT_NUMBER
        )
        if p is None or p.IsReadOnly:
            return False
        p.Set(new_number)
        return True
    except Exception:
        logger.debug("Circuit number write rejected for %s",
                      electrical_system.Id, exc_info=True)
        return False


def swap_schedule_slots(panel_schedule_view, slot_a, slot_b):
    """
    Moves the circuit occupying `slot_a` to `slot_b` using
    `CanMoveSlotTo` / `MoveSlotTo`. Per THIRD CORRECTION above, these
    take (row, col) cell addresses, not raw slot numbers - resolved
    here via `GetCellsBySlotNumber` first.

    NOTE (documented Revit API forum limitation, not our assumption):
    Revit will refuse a move if the destination cell is already
    occupied - there is no atomic "swap" call. This helper performs
    the simple case (destination free) and logs a warning otherwise,
    so the caller can plan around it (see services/arrangement_service
    .py, which builds its plan against free slots for this reason).
    """
    try:
        row_a, col_a = _resolve_cell(panel_schedule_view, slot_a)
        row_b, col_b = _resolve_cell(panel_schedule_view, slot_b)
        if row_a is None or row_b is None:
            return False
        if not panel_schedule_view.CanMoveSlotTo(row_a, col_a, row_b, col_b):
            logger.warning(
                "CanMoveSlotTo reports slot %s cannot move to slot %s on "
                "schedule '%s' (destination likely occupied or locked).",
                slot_a, slot_b, panel_schedule_view.Name
            )
            return False
        panel_schedule_view.MoveSlotTo(row_a, col_a, row_b, col_b)
        return True
    except Exception:
        logger.warning(
            "Could not move slot %s -> %s on schedule '%s'.",
            slot_a, slot_b, panel_schedule_view.Name, exc_info=True
        )
        return False


def _resolve_cell(panel_schedule_view, slot_number):
    """
    Resolves a 1-based slot number to its (row, col) cell address via
    `GetCellsBySlotNumber`, which returns parallel row[]/col[] arrays
    (use the primary/first cell). Returns (None, None) if the slot
    can't be resolved (e.g. out of range).
    """
    try:
        row_arr, col_arr = panel_schedule_view.GetCellsBySlotNumber(slot_number)
        if not row_arr or len(row_arr) == 0:
            return None, None
        return row_arr[0], col_arr[0]
    except Exception:
        return None, None


def add_spare_or_space(panel_schedule_view, slot_number, is_spare):
    """
    Fills an empty slot with a Spare or Space marker. Resolves the
    slot to a real (row, col) cell first (THIRD CORRECTION), then
    calls `AddSpare(row, col)` / `AddSpace(row, col)`. Returns the
    resulting circuit object (from `GetCircuitByCell`) on success, so
    the caller can set breaker rating / pole count on it, or None on
    failure.
    """
    row, col = _resolve_cell(panel_schedule_view, slot_number)
    if row is None:
        logger.warning(
            "Could not resolve slot %s to a cell on schedule '%s'.",
            slot_number, panel_schedule_view.Name
        )
        return None
    try:
        if is_spare:
            panel_schedule_view.AddSpare(row, col)
        else:
            panel_schedule_view.AddSpace(row, col)
        return panel_schedule_view.GetCircuitByCell(row, col)
    except Exception:
        logger.warning(
            "Could not add %s at slot %s (row=%s, col=%s) on schedule '%s'",
            "spare" if is_spare else "space", slot_number, row, col,
            panel_schedule_view.Name, exc_info=True
        )
        return None


def get_empty_slots(panel_schedule_view, total_slots, occupied_slots=None):
    """
    Returns a list of 1-based slot numbers that are currently empty
    (no circuit, spare or space assigned).

    Per THIRD CORRECTION, emptiness is checked via
    `GetCircuitByCell(row, col)` returning None - the reliable check
    confirmed against a proven prior extension - rather than the
    never-fully-verified `IsSpare`/`IsSpace` calls this used
    previously. `occupied_slots` (our own PanelInfo.circuits' slots,
    if already known) is still honored as an extra safety net. On any
    doubt (an exception reading a cell), the slot is treated as
    occupied so we never risk double-filling real data.
    """
    occupied = set(occupied_slots or [])
    empty = []
    for slot in range(1, total_slots + 1):
        if slot in occupied:
            continue
        try:
            row, col = _resolve_cell(panel_schedule_view, slot)
            if row is None:
                continue
            if panel_schedule_view.GetCircuitByCell(row, col) is not None:
                continue
            empty.append(slot)
        except Exception:
            continue
    return empty


def get_total_slots(panel_schedule_view, doc=None):
    """
    Returns the schedule's actual slot count via
    `GetTableData().NumberOfSlots` - confirmed against a proven prior
    extension as the direct, reliable way to read this (simpler than
    deriving it from the Body section's row count, which
    `get_schedule_body_row_count` above still provides as a fallback
    for callers that specifically need the raw table geometry).

    FOURTH CORRECTION: this function previously called
    `doc.Regenerate()` as a retry when `GetTableData()` returned None.
    That crashed Revit itself on at least one real model ("Failed to
    regenerate") rather than raising a normal, catchable exception -
    see `get_schedule_body_row_count`'s docstring for the full
    account. The `doc` parameter is kept for call-site compatibility
    but is intentionally UNUSED here now; no Regenerate() call is ever
    made from this read path. If the slot count still can't be read
    after the Body-row-count fallback, this is logged as an ERROR
    (not just a warning), since a 0 slot count means this panel will
    be effectively skipped by every downstream step (balancing/
    arrangement/spare-space) - the caller should treat a 0 return as
    "this panel needs manual attention" (e.g. a manual Regenerate/
    reload of the document via Revit's own UI), not silently proceed
    as if it had no capacity.
    """
    try:
        return panel_schedule_view.GetTableData().NumberOfSlots
    except Exception:
        logger.warning(
            "Could not read NumberOfSlots for schedule '%s'; falling back "
            "to Body row count.", panel_schedule_view.Name, exc_info=True
        )
        result = get_schedule_body_row_count(panel_schedule_view, doc=doc)
        if not result:
            logger.error(
                "Panel schedule '%s' returned 0 slots after every fallback "
                "- this panel will effectively be skipped by balancing, "
                "arrangement, and spare/space filling. This usually means "
                "the schedule's underlying panel element is invalid/deleted, "
                "or the document needs a manual Regenerate/reload. Check "
                "this panel manually before trusting its results.",
                panel_schedule_view.Name
            )
        return result


def count_connected_ways(panel_schedule_view, total_slots):
    """
    Counts how many of the panel's current slots are occupied by a
    REAL connected circuit - excluding any Spare/Space already present
    - so the count reflects actual connected circuits, matching the
    "Connected Ways" concept used by the Ways-bracket lookup in
    services/spare_space_service.get_spare_space_counts_from_ways.
    """
    occupied = 0
    for slot in range(1, total_slots + 1):
        row, col = _resolve_cell(panel_schedule_view, slot)
        if row is None:
            continue
        try:
            circuit = panel_schedule_view.GetCircuitByCell(row, col)
        except Exception:
            continue
        if circuit is None:
            continue
        load_name = ""
        try:
            load_name = (circuit.LoadName or "").strip().upper()
        except Exception:
            pass
        if load_name in ("SPARE", "SPACE"):
            continue
        occupied += 1
    return occupied


# ---------------------------------------------------------------------------
# Panel capacity ("ways") - reading and increasing
# ---------------------------------------------------------------------------


def get_capacity_param(panel_element):
    """
    Returns the panel's slot-capacity instance parameter, trying each
    known display name in turn (Revit has renamed this over the years,
    and switchboards use a different name than panelboards). Returns
    None if none of them exist on this panel's family.
    """
    if panel_element is None:
        return None
    for pname in CAPACITY_PARAM_NAMES:
        p = panel_element.LookupParameter(pname)
        if p is not None:
            return p
    return None


def _get_highest_occupied_slot(panel_schedule_view, total_slots):
    """
    Returns the highest 1-based slot number currently occupied by a
    REAL connected circuit - excluding existing Spare/Space
    placeholders, since those are exactly what this same operation is
    about to redefine anyway - or 0 if none. Used by
    `bump_panel_capacity` to guarantee capacity is never shrunk below
    what's actually needed to keep existing real circuits intact.
    """
    highest = 0
    for slot in range(1, total_slots + 1):
        row, col = _resolve_cell(panel_schedule_view, slot)
        if row is None:
            continue
        try:
            circuit = panel_schedule_view.GetCircuitByCell(row, col)
        except Exception:
            continue
        if circuit is None:
            continue
        load_name = ""
        try:
            load_name = (circuit.LoadName or "").strip().upper()
        except Exception:
            pass
        if load_name in ("SPARE", "SPACE"):
            continue
        highest = slot
    return highest


def bump_panel_capacity(doc, panel_schedule_view, panel_element, panel_name,
                         new_capacity, current_slots, log_fn=None):
    """
    Adjusts the panel's slot capacity parameter to `new_capacity` -
    growing OR shrinking as needed - regenerates, and re-checks the
    schedule's ACTUAL slot count afterward (since a schedule TEMPLATE
    set to "Fixed" rather than "Variable based on max number of single
    pole breakers" won't actually change even after the parameter
    changes - this is detected and logged rather than silently
    under/over-filling). Must be called from inside an open
    Transaction. Returns the resulting slot count (unchanged from
    `current_slots` if the change had no effect).

    ELEVENTH CORRECTION: this used to only ever GROW capacity (a no-op
    whenever `new_capacity <= current_slots`), so a panel whose
    family/template default capacity was larger than actually needed
    (e.g. a generic panel type defaulting to a certain slot count when
    the panel only has a couple of connected circuits) was left
    oversized instead of "right-sized" down to the smallest sufficient
    standard bracket - confirmed live: a 2-circuit panel that should
    have shrunk to the 12-way bracket stayed at its starting capacity
    instead. Shrinking is now supported, but NEVER below the highest
    slot currently occupied by a REAL connected circuit
    (`_get_highest_occupied_slot`), so existing circuit data can never
    be destroyed by a shrink.
    """
    log_fn = log_fn or (lambda msg: None)
    if new_capacity == current_slots:
        return current_slots

    if new_capacity < current_slots:
        highest_occupied = _get_highest_occupied_slot(panel_schedule_view, current_slots)
        safe_target = max(new_capacity, highest_occupied)
        if safe_target != new_capacity:
            log_fn(
                "Panel '{}': wanted to shrink capacity to {} ways, but "
                "slot {} already has a real connected circuit - using {} "
                "instead to avoid losing existing circuit data.".format(
                    panel_name, new_capacity, highest_occupied, safe_target)
            )
        new_capacity = safe_target
        if new_capacity == current_slots:
            return current_slots

    cap_param = get_capacity_param(panel_element)
    if cap_param is None or cap_param.IsReadOnly:
        direction = "increase" if new_capacity > current_slots else "reduce"
        log_fn(
            "Panel '{}': capacity is {} but {} ways would be more "
            "appropriate, and no editable breaker-count parameter was "
            "found on this panel's family - {} it manually in the "
            "panel's instance properties, then re-run.".format(
                panel_name, current_slots, new_capacity, direction)
        )
        return current_slots

    cap_param.Set(new_capacity)
    try:
        doc.Regenerate()
    except Exception as ex:
        # Same category of failure documented at length in
        # create_or_get_panel_schedule_view's module-docstring
        # corrections (SEVENTH/EIGHTH/TENTH) - an explicit
        # mid-transaction Regenerate() call has been observed to fail
        # unreliably in some projects/templates. Unlike schedule
        # creation, there is no alternative "commit and rely on
        # implicit regeneration" restructuring available here (this
        # call is deliberately mid-placement-loop, inside the panel's
        # single "Update Panel Schedule" transaction), so this is
        # surfaced as clearly as possible instead: the capacity
        # parameter WAS set, but Revit's own regeneration could not
        # confirm the effect took hold.
        log_fn(
            "Panel '{}': set the capacity parameter to {}, but Revit's "
            "own regeneration failed while confirming the change ({}). "
            "The parameter value may or may not have actually taken "
            "effect - check this panel manually.".format(
                panel_name, new_capacity, ex)
        )
        return current_slots
    new_total_slots = get_total_slots(panel_schedule_view, doc=doc)

    if new_total_slots == current_slots:
        log_fn(
            "Panel '{}': set the capacity parameter to {}, but the "
            "SCHEDULE still shows {} slots. Its panel schedule TEMPLATE "
            "likely has 'Number of slots shown' set to Fixed instead of "
            "Variable - edit the template (General Settings > Number of "
            "slots shown > Variable based on max number of single pole "
            "breakers), then re-run.".format(
                panel_name, new_capacity, current_slots)
        )
    else:
        direction = "Increased" if new_total_slots > current_slots else "Reduced"
        log_fn(
            "Panel '{}': {} capacity to {} ways to match connected "
            "circuits plus the planned spare/space.".format(
                panel_name, direction.lower(), new_total_slots)
        )
    return new_total_slots


# ---------------------------------------------------------------------------
# Silent failure handling (suppresses Revit's own warning/error dialogs
# during batch spare/space placement)
# ---------------------------------------------------------------------------

class SilentFailuresPreprocessor(IFailuresPreprocessor):
    """
    Prevents Revit's own failure/warning dialogs from popping up and
    blocking the script during batch spare/space placement. Warnings
    are dismissed automatically; anything that "must be resolved"
    (e.g. not enough consecutive empty slots to expand a multi-pole
    spare/space into) rolls back just the pending (sub)transaction
    instead of showing a modal dialog. Ported from a proven prior
    extension. Attach via
    `transaction.GetFailureHandlingOptions().SetFailuresPreprocessor(...)`.
    """

    def __init__(self):
        # FIFTIETH CORRECTION: remember what Revit objected to, so a
        # transaction this class rolls back can say WHY (see
        # RevitTransaction.__exit__). It used to roll back in silence.
        self.errors = []

    def PreprocessFailures(self, failures_accessor):
        failures = list(failures_accessor.GetFailureMessages())
        if not failures:
            return FailureProcessingResult.Continue

        blocking = False
        for f in failures:
            if f.GetSeverity() == FailureSeverity.Warning:
                try:
                    failures_accessor.DeleteWarning(f)
                except Exception:
                    pass
            else:
                blocking = True
                try:
                    self.errors.append(f.GetDescriptionText())
                except Exception:
                    self.errors.append("(Revit error without a description)")

        if blocking:
            failures_accessor.RollBackPendingTransaction()
            return FailureProcessingResult.ProceedWithRollBack

        return FailureProcessingResult.Continue


# ---------------------------------------------------------------------------
# Physical (row/column) cell mapping for phase-aligned multi-pole placement
# ---------------------------------------------------------------------------

def _build_cell_map(panel_schedule_view, n_slots):
    """
    Maps every slot to its physical (row, col), and builds an
    occupancy lookup keyed by (row, col) - so multi-pole runs can be
    checked as genuine consecutive ROWS in the SAME COLUMN (how a
    breaker physically stacks), not just consecutive slot numbers
    (which don't reliably correspond to physical adjacency once the
    panel already has a mix of 1/2/3-pole circuits in it). Returns
    (all_cells, occupied_by_rc, base_row); all_cells is sorted
    column-major, top-to-bottom (matches how a breaker expands
    downward within one column).
    """
    all_cells = []
    occupied_by_rc = {}
    for slot in range(1, n_slots + 1):
        row, col = _resolve_cell(panel_schedule_view, slot)
        if row is None:
            continue
        if (row, col) in occupied_by_rc:
            continue
        all_cells.append((row, col))
        try:
            occupied_by_rc[(row, col)] = \
                panel_schedule_view.GetCircuitByCell(row, col) is not None
        except Exception:
            occupied_by_rc[(row, col)] = True
    all_cells.sort(key=lambda rc: (rc[1], rc[0]))
    rows = [r for r, c in all_cells]
    base_row = min(rows) if rows else 0
    return all_cells, occupied_by_rc, base_row


def _find_free_run(all_cells, occupied_by_rc, base_row, n_poles, start_idx,
                    require_phase_align=False):
    """
    First index into all_cells whose row starts a genuine run of
    `n_poles` consecutive FREE rows in the SAME column, at or after
    start_idx, or None. When require_phase_align is True, only
    considers starting rows where (row - base_row) % 3 == 0 - the "R"
    position in the R/Y/B repeat cycle, so a 3-pole spare/space
    naturally reads R, Y, B in order.
    """
    for idx in range(start_idx, len(all_cells)):
        row, col = all_cells[idx]
        if require_phase_align and ((row - base_row) % 3 != 0):
            continue
        ok = True
        for k in range(n_poles):
            rc = (row + k, col)
            if occupied_by_rc.get(rc, True):
                ok = False
                break
        if ok:
            return idx
    return None


def _free_cell_on_leg(all_cells, occupied_by_rc, base_row, leg, phase_labels):
    """Index of the first free single cell whose row is on `leg`, or None."""
    for idx, (row, col) in enumerate(all_cells):
        if occupied_by_rc.get((row, col), True):
            continue
        if phase_for_row(row, base_row, phase_labels) == leg:
            return idx
    return None


def _place_one_spare_or_space(doc, panel_schedule_view, panel_name, kind,
                               n_poles, spare_rating, all_cells,
                               occupied_by_rc, base_row, start_idx, log_fn,
                               placements=None, prefer_leg=None, phase_labels=None):
    """
    Places a single Spare or Space at the first physically-valid free
    run found at/after start_idx, wrapped in its own SubTransaction
    with SilentFailuresPreprocessor so a Revit-side rejection (e.g. an
    unexpected layout edge case) rolls back just this one placement
    instead of failing the whole batch. Sets pole count on the
    resulting circuit, and breaker rating too (spare only - a Space
    has no physical breaker, so it intentionally gets none). Returns
    the index to resume searching from on success, "retry_after" to
    skip this position and keep trying, or None if no room is left at
    all.
    """
    phase_aligned = False
    idx = None
    if n_poles == 1 and prefer_leg is not None and phase_labels:
        # FIFTY-FIFTH CORRECTION: put this single-pole spare on the leg asked for
        idx = _free_cell_on_leg(all_cells, occupied_by_rc, base_row, prefer_leg, phase_labels)
    if idx is None and n_poles == 3:
        idx = _find_free_run(all_cells, occupied_by_rc, base_row,
                              n_poles, start_idx, require_phase_align=True)
        phase_aligned = idx is not None
    if idx is None:
        idx = _find_free_run(all_cells, occupied_by_rc, base_row,
                              n_poles, start_idx, require_phase_align=False)
    if idx is None:
        return None
    row, col = all_cells[idx]

    sub = SubTransaction(doc)
    sub.Start()
    try:
        if kind == "spare":
            panel_schedule_view.AddSpare(row, col)
        else:
            panel_schedule_view.AddSpace(row, col)

        circuit = panel_schedule_view.GetCircuitByCell(row, col)
        if circuit is not None:
            poles_param = circuit.get_Parameter(
                BuiltInParameter.RBS_ELEC_NUMBER_OF_POLES
            )
            if poles_param and not poles_param.IsReadOnly:
                poles_param.Set(n_poles)
            if kind == "spare":
                rating_param = circuit.get_Parameter(
                    BuiltInParameter.RBS_ELEC_CIRCUIT_RATING_PARAM
                )
                if rating_param and not rating_param.IsReadOnly:
                    rating_param.Set(spare_rating)
                # Space intentionally gets no rating - no breaker installed.

        status = sub.Commit()
    except Exception as ex:
        if not sub.HasEnded():
            sub.RollBack()
        log_fn("Panel '{}': failed to add {} at row {} col {} ({})".format(
            panel_name, kind, row, col, ex))
        return "retry_after"

    if str(status) != "Committed":
        log_fn(
            "Panel '{}': Revit rejected {} at row {} col {} (not enough "
            "contiguous room) - trying next slot.".format(
                panel_name, kind, row, col)
        )
        return "retry_after"

    for k in range(n_poles):
        occupied_by_rc[(row + k, col)] = True
    # TWENTY-SEVENTH CORRECTION: report exactly which cell this
    # spare/space landed in (and how many rows it spans) so the caller
    # can afterwards write per-leg loads onto the spares with
    # `set_spare_slot_loads`. Without this the placements were
    # write-only - nothing downstream knew where they ended up.
    if placements is not None:
        placements.append({
            "row": row, "col": col, "poles": n_poles,
            "kind": kind, "phase_aligned": phase_aligned,
        })
    if n_poles == 3 and not phase_aligned:
        log_fn(
            "Panel '{}': placed a 3-pole {} at row {} col {} but no "
            "R-aligned run was free - phase order there may not read "
            "R-Y-B. Check that circuit if exact phase order matters.".format(
                panel_name, kind, row, col)
        )
    return idx + 1


def _place_remaining(doc, panel_schedule_view, panel_name, kind, n_poles,
                      spare_rating, needed, already_placed, all_cells,
                      occupied_by_rc, base_row, log_fn, placements=None,
                      phase_labels=None):
    """
    FIFTY-FIFTH CORRECTION: `phase_labels` given -> single-pole SPARES are
    spread over the legs (each new spare goes on the leg with the fewest
    spares so far). They used to fill the first free cells in column
    order, so on a live panel every new spare landed on R and Y; the
    spare load booked onto them then pushed a 4.4% board to 16.9% on the
    schedule, with nothing on B to balance against.
    """
    placed = already_placed
    cursor = 0
    spread = kind == "spare" and n_poles == 1 and phase_labels
    per_leg = dict((leg, 0) for leg in (phase_labels or []))
    if spread:
        for p in (placements or []):
            if p.get("kind") == "spare" and p.get("poles", 1) == 1:
                leg = phase_for_row(p["row"], base_row, phase_labels)
                per_leg[leg] = per_leg.get(leg, 0) + 1
    tried_legs = set()
    while placed < needed:
        prefer = None
        if spread:
            candidates = [leg for leg in phase_labels if leg not in tried_legs]
            if candidates:
                prefer = min(candidates, key=lambda leg: (per_leg.get(leg, 0), list(phase_labels).index(leg)))
        result = _place_one_spare_or_space(
            doc, panel_schedule_view, panel_name, kind, n_poles,
            spare_rating, all_cells, occupied_by_rc, base_row, cursor, log_fn,
            placements=placements, prefer_leg=prefer, phase_labels=phase_labels
        )
        if result is None:
            if prefer is not None:
                tried_legs.add(prefer)       # no room on that leg - try the next one
                continue
            break
        if result == "retry_after":
            if prefer is not None:
                tried_legs.add(prefer)
            else:
                cursor += 1
            continue
        if not spread:
            cursor = result
        placed += 1
        if spread and placements:
            leg = phase_for_row(placements[-1]["row"], base_row, phase_labels)
            per_leg[leg] = per_leg.get(leg, 0) + 1
            tried_legs = set()
    return placed


def fill_spare_and_space(doc, panel_element, panel_schedule_view, panel_name,
                          spare_needed, space_needed, spare_rating, poles,
                          ways_table, log_fn=None, preserve_capacity=False,
                          phase_labels=None):
    """
    The Revit-facing counterpart of
    services.spare_space_service.get_spare_space_counts_from_ways /
    pick_standard_capacity. Ported from a proven prior extension
    (SpareSpaceFiller): applies Spare/Space to a single panel schedule,
    RIGHT-SIZING its slot capacity first (shrinking it down to the
    smallest sufficient standard "ways" bracket if it started larger
    than actually needed, or growing it if it started smaller - see
    ELEVENTH CORRECTION on `bump_panel_capacity`), using phase-aligned
    (R-Y-B) placement for 3-pole items, and automatically stepping up
    to the next larger standard "ways" size if the smallest
    numerically-sufficient one turns out to be too fragmented for
    genuinely consecutive room. Any slot still empty afterward within
    the panel's final capacity is also filled as Space, so there's
    never a truly blank leftover row. Must be called from inside an
    open Transaction (spare/space placements each run in their own
    SubTransaction internally).

    Returns (spare_placed, space_placed, spare_cells), where
    `spare_cells` is a list of
        {"row": int, "col": int, "poles": int, "phases": [phase, ...]}
    for every SPARE actually placed (TWENTY-SEVENTH CORRECTION - so
    the caller can write balanced per-leg loads onto them with
    `set_spare_slot_loads`). Spaces are deliberately excluded: a Space
    has no breaker, so it reserves no capacity and carries no load.

    THIRTY-FIRST CORRECTION: `preserve_capacity=True` skips the
    `bump_panel_capacity` step entirely, so an existing, issued
    schedule is never grown or shrunk underneath the user - spares and
    spaces are fitted into the free slots the panel already has, and a
    shortfall is reported instead of silently resizing the board.
    """
    log_fn = log_fn or (lambda msg: None)
    spare_cells = []

    total_slots = get_total_slots(panel_schedule_view, doc=doc)
    connected = count_connected_ways(panel_schedule_view, total_slots)
    # Each spare/space reserves `poles` physical slots, not 1.
    raw_target = connected + (spare_needed * poles) + (space_needed * poles)

    from panelmgr.services.spare_space_service import pick_standard_capacity
    candidates = pick_standard_capacity(ways_table, raw_target)

    spare_placed, space_placed = 0, 0
    all_cells, occupied_by_rc, base_row = [], {}, 0
    if preserve_capacity:
        # Only ever consider the capacity the panel already has.
        candidates = [total_slots]

    for idx, candidate_capacity in enumerate(candidates):
        if not preserve_capacity:
            total_slots = bump_panel_capacity(
                doc, panel_schedule_view, panel_element, panel_name,
                candidate_capacity, total_slots, log_fn
            )
        all_cells, occupied_by_rc, base_row = _build_cell_map(
            panel_schedule_view, total_slots
        )

        spare_placed = _place_remaining(
            doc, panel_schedule_view, panel_name, "spare", poles,
            spare_rating, spare_needed, spare_placed,
            all_cells, occupied_by_rc, base_row, log_fn,
            placements=spare_cells,
            phase_labels=list(phase_labels) if phase_labels else ["R", "Y", "B"],
        )
        space_placed = _place_remaining(
            doc, panel_schedule_view, panel_name, "space", poles,
            spare_rating, space_needed, space_placed,
            all_cells, occupied_by_rc, base_row, log_fn
        )

        if spare_placed >= spare_needed and space_placed >= space_needed:
            break
        if idx < len(candidates) - 1:
            log_fn(
                "Panel '{}': {} ways wasn't enough contiguous room "
                "(fragmented free slots) - stepping up to the next "
                "standard size ({}).".format(
                    panel_name, candidate_capacity, candidates[idx + 1])
            )

    if spare_placed < spare_needed or space_placed < space_needed:
        log_fn(
            "Panel '{}': still short after trying every standard size up "
            "to {} ways - add a bigger row to the Ways table, or check for "
            "heavily fragmented empty slots.".format(
                panel_name, candidates[-1])
        )

    # Fill any slot still empty within the final capacity as Space, so
    # there's never a truly blank leftover row.
    all_cells, occupied_by_rc, base_row = _build_cell_map(
        panel_schedule_view, total_slots
    )
    extra_space_placed = _place_remaining(
        doc, panel_schedule_view, panel_name, "space", 1, spare_rating,
        len(all_cells), 0, all_cells, occupied_by_rc, base_row, log_fn
    )
    if extra_space_placed:
        log_fn("Panel '{}': also filled {} leftover empty slot(s) as "
               "Space.".format(panel_name, extra_space_placed))
        space_placed += extra_space_placed

    # Annotate each placed spare with the phase leg(s) its rows sit on,
    # using the same row-based R-Y-B model `_find_free_run` uses for
    # phase-aligned placement (see `phase_for_row`).
    labels = list(phase_labels) if phase_labels else ["R", "Y", "B"]
    for cell in spare_cells:
        cell["phases"] = [
            phase_for_row(cell["row"] + k, base_row, labels)
            for k in range(cell.get("poles", 1))
        ]

    return spare_placed, space_placed, spare_cells


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------

class RevitTransaction(object):
    """
    Context manager wrapper around Autodesk.Revit.DB.Transaction. By
    default attaches `SilentFailuresPreprocessor` (see above) so
    Revit's own warning/error dialogs never block the batch - warnings
    are dismissed automatically, and anything blocking rolls back just
    the pending transaction rather than showing a modal. Set
    `silent_failures=False` to opt out and let Revit's normal dialogs
    show (useful for interactive debugging).
    """

    def __init__(self, doc, name, silent_failures=True):
        self._doc = doc
        self._name = name
        self._silent_failures = silent_failures
        self._preprocessor = None
        self._t = Transaction(doc, name)

    def _apply_failure_options(self):
        """
        THIRTY-SEVENTH CORRECTION (this class never actually worked):
        the failure-handling options were configured in `__init__`,
        i.e. BEFORE `Transaction.Start()`. The Revit API documents
        that "every time a transaction starts, Failure Handling
        Options will be reset to their default values", so Start()
        threw the preprocessor away every single time and the batch
        ran with Revit's stock modal dialogs - the exact thing this
        class exists to prevent. Because the original code swallowed
        any exception into a debug-level warning, there was no visible
        symptom other than dialogs appearing mid-batch and blocking a
        run that was supposed to be unattended.

        Also note `SetFailuresPreprocessor` / `SetForcedModalHandling`
        return the modified options object rather than mutating in
        place on some API versions, so the return value is used where
        available instead of relying on mutation.
        """
        if not self._silent_failures:
            return
        try:
            fho = self._t.GetFailureHandlingOptions()
            self._preprocessor = SilentFailuresPreprocessor()
            fho = fho.SetFailuresPreprocessor(self._preprocessor) or fho
            fho = fho.SetForcedModalHandling(False) or fho
            self._t.SetFailureHandlingOptions(fho)
        except Exception:
            logger.warning(
                "Could not attach SilentFailuresPreprocessor to "
                "transaction '%s'; Revit's normal dialogs may appear.",
                self._name, exc_info=True
            )

    def __enter__(self):
        self._t.Start()
        # MUST come after Start() - see _apply_failure_options.
        self._apply_failure_options()
        return self._t

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            # FIFTIETH CORRECTION (this is why a "successful" run could
            # change nothing): `Transaction.Commit()` does not raise when
            # Revit rejects the changes - it RETURNS a status. With the
            # silent failure handler attached, any error-level failure
            # during commit rolls the whole transaction back, and this
            # used to ignore the status and carry on as if everything
            # had been written. Every circuit move and every spare of
            # that panel was undone while the results grid reported the
            # balanced figures; and a schedule created in such a
            # transaction no longer existed, which is the
            # "referenced object is not valid ... its creation was
            # undone" failure in the log. A commit that does not commit
            # is now an error for that panel, with Revit's own reason.
            status = self._t.Commit()
            if status != TransactionStatus.Committed:
                reasons = list(getattr(self._preprocessor, "errors", None) or [])
                message = (
                    "Revit did not accept the changes of '{}' and rolled them "
                    "back (status: {}).{}".format(
                        self._name, status,
                        " Revit's reason: " + " | ".join(reasons) if reasons
                        else " Revit gave no reason.")
                )
                logger.error(message)
                raise RuntimeError(message)
        else:
            logger.error("Rolling back transaction '%s' due to %s: %s",
                         self._name, exc_type, exc_val)
            self._t.RollBack()
        return False  # do not suppress exceptions


class RevitTransactionGroup(object):
    """
    Wraps the whole batch (balance + arrange + spare/space + schedule
    creation across N panels) in a single TransactionGroup so the user
    can Undo the entire operation in one step, and so the "Cancel"
    button in the wizard can roll back everything cleanly if the user
    aborts mid-batch.
    """

    def __init__(self, doc, name):
        self._doc = doc
        self._tg = TransactionGroup(doc, name)
        self._assimilated = False

    def __enter__(self):
        self._tg.Start()
        return self

    def rollback(self):
        self._tg.RollBack()
        self._assimilated = True

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._assimilated:
            return False
        if exc_type is None:
            self._tg.Assimilate()
        else:
            self._tg.RollBack()
        return False

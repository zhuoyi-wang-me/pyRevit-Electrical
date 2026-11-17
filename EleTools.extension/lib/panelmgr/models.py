# -*- coding: utf-8 -*-
"""
Plain-old data classes shared across services and view models.

None of these classes import the Revit API directly. Revit `ElementId`
values are stored as opaque integers (`element_id`) so that the
balancing / arrangement / spare-space algorithms can be unit tested
with plain Python and fixture data (see /tests in the README).
"""


PHASES = ("R", "Y", "B")

SINGLE_PHASE = "SinglePhase"
THREE_PHASE = "ThreePhase"


class SlotLayout(object):
    """
    FORTY-FIFTH CORRECTION. How a panel schedule's slot numbers map to
    phase legs and to physical position.

    Every earlier revision hard-coded ONE layout: two columns, odd slots
    left / even slots right, two slots per phase - slots (1,2)=R,
    (3,4)=Y, (5,6)=B - and a multi-pole breaker stepping by 2. That is
    only true for a two-column template. On a ONE-column template (the
    usual R1/Y1/B1 "distribution board" schedule) slot 1=R, 2=Y, 3=B and
    a 3-pole breaker occupies slots n, n+1, n+2. With the wrong model the
    "before" phase totals were fiction, the balancing plan moved circuits
    to slots on the wrong leg, and a 3-pole circuit was thought to occupy
    slots it does not.

    The layout is now READ from the schedule (see
    `revit_utils.read_slot_cells` and `build_layout`): a slot's leg comes
    from the ROW its cell sits on, and "the slot below" is the slot whose
    cell is one row down in the same column. When no schedule exists yet
    (Preview of a panel without one) the old two-column formula is still
    used as the assumption, and `source` says so.

    Attributes:
        labels      leg names, ("R","Y","B") or ("L1","L2")
        slot_phase  {slot: leg} or None (None = two-column formula)
        slot_below  {slot: slot one row down, same column} or None
        source      "schedule" / "assumed"
        confidence  share (0..1) of single-pole circuits whose REAL phase
                    (from Revit's per-phase load) agrees with this layout,
                    or None when there was nothing to check against
        checked     how many circuits that share was measured on
    """

    def __init__(self, labels=None, slot_phase=None, slot_below=None,
                 source="assumed", confidence=None, checked=0):
        self.labels = tuple(labels) if labels else PHASES
        self.slot_phase = slot_phase
        self.slot_below = slot_below
        self.source = source
        self.confidence = confidence
        self.checked = checked

    def phase(self, slot):
        """Leg of a 1-based slot, or None if the slot is not in the schedule."""
        if self.slot_phase is not None:
            return self.slot_phase.get(slot)
        return self.labels[((slot - 1) // 2) % len(self.labels)]

    def run(self, start_slot, poles, strict=False):
        """
        The slots a `poles`-pole breaker starting at `start_slot` occupies:
        the start slot and the slots directly below it.

        strict=True returns None when the run would leave the schedule
        (used when SEARCHING for a place); strict=False returns the part
        that exists (used for occupancy bookkeeping).
        """
        poles = max(1, int(poles or 1))
        if self.slot_below is None:
            return [start_slot + 2 * k for k in range(poles)]
        out = [start_slot]
        current = start_slot
        for _ in range(poles - 1):
            current = self.slot_below.get(current)
            if current is None:
                return None if strict else out
            out.append(current)
        return out

    def trusted(self, minimum=0.8):
        """False when real circuit phases contradict this layout."""
        return self.confidence is None or self.confidence >= minimum


def build_layout(cells, labels, evidence=None):
    """
    Builds a SlotLayout from a schedule's real cell positions. Pure
    Python (unit-testable); the Revit reads are done by the caller.

        cells     {slot: (row, col)} from GetCellsBySlotNumber
        labels    leg names of the panel
        evidence  [(slot, leg)] for single-pole circuits whose real leg is
                  known from Revit's own per-phase load. Used to find which
                  leg the FIRST row is on (normally the first label, but
                  this is checked rather than assumed) and to measure how
                  well the layout matches reality.
    """
    labels = tuple(labels) if labels else PHASES
    if not cells:
        return SlotLayout(labels=labels)
    n = len(labels)
    base_row = min(row for row, _col in cells.values())

    offset, hits, total = 0, 0, 0
    usable = [(slot, leg) for slot, leg in (evidence or [])
              if slot in cells and leg in labels]
    if usable:
        total = len(usable)
        best = -1
        for candidate in range(n):
            score = len([1 for slot, leg in usable
                         if labels[(cells[slot][0] - base_row + candidate) % n] == leg])
            if score > best:
                best, offset = score, candidate
        hits = best

    by_cell = {}
    for slot in sorted(cells):
        by_cell.setdefault(cells[slot], slot)
    slot_phase = dict((slot, labels[(row - base_row + offset) % n])
                      for slot, (row, _col) in cells.items())
    slot_below = dict((slot, by_cell.get((row + 1, col)))
                      for slot, (row, col) in cells.items())
    return SlotLayout(labels=labels, slot_phase=slot_phase, slot_below=slot_below,
                      source="schedule",
                      confidence=(hits / float(total)) if total else None,
                      checked=total)


class CircuitInfo(object):
    """
    Represents one electrical circuit (branch circuit) on a panel.

    Attributes:
        element_id (int): Revit ElementId.IntegerValue of the
            ElectricalSystem.
        circuit_number (str): Current circuit number as shown on the
            panel schedule, e.g. "12" or "12,14,16" for a multi-pole
            circuit that is not yet numbered, this may be empty.
        description (str): Load name / description.
        poles (int): 1, 2 or 3.
        apparent_load_va (float): Apparent load in VA.
        load_classification (str): e.g. "Lighting", "Receptacle",
            "Motor", "Mechanical", "Spare", "Space".
        phases (tuple): Which phase legs this circuit occupies, e.g.
            ("R",) for a 1-pole circuit or ("R", "Y", "B") for 3-pole.
            Populated / re-assigned by the balancing service.
        slot (int): Physical slot (row) number in the panel schedule,
            1-based. Populated / re-assigned by the arrangement
            service.
        is_spare (bool)
        is_space (bool)
        locked (bool): If True, the balancing/arrangement engine must
            not move this circuit (user pinned it in the Preview
            step).
    """

    __slots__ = (
        "element_id", "circuit_number", "description", "poles",
        "apparent_load_va", "true_load_w", "load_classification", "phases",
        "slot", "is_spare", "is_space", "locked", "phase_va",
    )

    def __init__(self, element_id, circuit_number="", description="",
                 poles=1, apparent_load_va=0.0, true_load_w=None,
                 load_classification="", phases=None, slot=None,
                 is_spare=False, is_space=False, locked=False, phase_va=None):
        # FORTY-FIFTH CORRECTION: Revit's OWN per-phase apparent load for
        # this circuit, (A, B, C) in VA, or None when it could not be
        # read. This is the ground truth for which leg a circuit is on;
        # `phases` (derived from the slot) is checked against it.
        self.phase_va = tuple(phase_va) if phase_va else None
        self.element_id = element_id
        self.circuit_number = circuit_number
        self.description = description
        self.poles = poles
        self.apparent_load_va = apparent_load_va
        # TWENTIETH CORRECTION: Apparent Load (VA) and Real/True Load
        # (Watts) are genuinely different physical quantities related
        # by power factor (W = VA x PF) - a user correctly pointed out
        # our reports were showing VA where Watts were expected. VA is
        # what Revit's branch-circuit "Apparent Load" parameter stores
        # natively, and is also the electrically correct basis for
        # PHASE BALANCING itself (balancing equalizes current draw
        # across legs, which tracks VA, not real power) - so balancing
        # math continues to use apparent_load_va unchanged. This field
        # is a SEPARATE, Watts-based value used only for reporting,
        # computed as apparent_load_va * power_factor when a Power
        # Factor is available on the circuit (see revit_utils
        # ._build_circuit_info), falling back to apparent_load_va
        # itself (implying PF=1.0) when no Power Factor parameter can
        # be read - an approximation, not a replication of Revit's own
        # category-based demand-factor Watts calculations (visible in
        # a project's printed schedule "CONN./DEMAND WATTS" columns),
        # which depend on project-wide Electrical Settings this tool
        # does not have access to.
        self.true_load_w = true_load_w if true_load_w is not None else apparent_load_va
        self.load_classification = load_classification
        self.phases = tuple(phases) if phases else tuple()
        self.slot = slot
        self.is_spare = is_spare
        self.is_space = is_space
        self.locked = locked

    def phase_load_va(self, phase):
        """
        The load this circuit contributes to ONE phase leg.

        TWENTY-FIFTH CORRECTION (critical, silently wrong since v1):
        `apparent_load_va` is the circuit's TOTAL apparent load across
        all of its poles - it is NOT a per-leg figure. Both
        `_fixed_multipole_loads` and `compute_phase_loads` used to add
        the FULL value to every phase the circuit touches, so a 3-pole
        9 kVA circuit was counted as 9 kVA on R *and* 9 kVA on Y *and*
        9 kVA on B - 27 kVA of phantom load, a 3x inflation. This is
        not merely a reporting error the way the TWENTY-SECOND
        CORRECTION's unit-scaling was: because the inflation factor
        differs per circuit (x3 for 3-pole, x2 for 2-pole, x1 for
        1-pole), it does NOT cancel out of the relative comparison,
        so it actively skewed which phase the LPT heuristic believed
        was lightest and therefore produced genuinely wrong balancing
        decisions on any panel with a mix of pole counts.

        Splitting evenly across the legs the circuit occupies matches
        how Revit itself populates RBS_ELEC_APPARENT_LOAD_PHASEA/B/C.
        """
        if not self.phases or phase not in self.phases:
            return 0.0
        return self.apparent_load_va / float(len(self.phases))

    def phase_load_w(self, phase):
        """Watts counterpart of `phase_load_va` (reporting only)."""
        if not self.phases or phase not in self.phases:
            return 0.0
        return self.true_load_w / float(len(self.phases))

    def is_placeholder(self):
        """True for Spare or Space rows - never real connected load."""
        return bool(self.is_spare or self.is_space)

    def clone(self):
        return CircuitInfo(
            element_id=self.element_id,
            circuit_number=self.circuit_number,
            description=self.description,
            poles=self.poles,
            apparent_load_va=self.apparent_load_va,
            true_load_w=self.true_load_w,
            load_classification=self.load_classification,
            phases=self.phases,
            slot=self.slot,
            is_spare=self.is_spare,
            is_space=self.is_space,
            locked=self.locked,
            phase_va=self.phase_va,
        )

    def actual_legs(self, labels):
        """Legs this circuit REALLY loads, from Revit's per-phase figures
        (phase A -> first label, B -> second, C -> third). Empty when
        unknown or when the circuit carries no load."""
        if not self.phase_va:
            return tuple()
        return tuple(labels[i] for i, va in enumerate(self.phase_va)
                     if i < len(labels) and va and va > 0.5)

    def __repr__(self):
        return "<Circuit {} '{}' {}P {:.0f}VA phases={}>".format(
            self.circuit_number or "?", self.description, self.poles,
            self.apparent_load_va, self.phases
        )


class PanelInfo(object):
    """
    Represents one electrical panel (distribution board).

    Attributes:
        element_id (int): Revit ElementId.IntegerValue of the panel
            FamilyInstance.
        name (str): Panel name / "Panel Name" parameter.
        phase_config (str): SINGLE_PHASE or THREE_PHASE.
        voltage (float)
        num_slots (int): Total number of physical circuit slots
            (poles) available in the panel enclosure.
        circuits (list[CircuitInfo])
        panel_schedule_view_id (int|None): ElementId of the
            PanelScheduleView, if one already exists.
    """

    __slots__ = (
        "element_id", "name", "phase_config", "voltage", "num_slots",
        "circuits", "panel_schedule_view_id", "schedule_pre_existed", "layout",
    )

    def __init__(self, element_id, name, phase_config=THREE_PHASE,
                 voltage=208.0, num_slots=42, circuits=None,
                 panel_schedule_view_id=None, schedule_pre_existed=False,
                 layout=None):
        # SlotLayout read from the real schedule, or None until one exists.
        self.layout = layout
        self.element_id = element_id
        self.name = name
        self.phase_config = phase_config
        self.voltage = voltage
        self.num_slots = num_slots
        self.circuits = circuits if circuits is not None else []
        self.panel_schedule_view_id = panel_schedule_view_id
        # TWENTY-SIXTH CORRECTION: set True when a PanelScheduleView
        # already existed for this panel BEFORE this run started (as
        # opposed to one this tool created). This is the flag that
        # `BalancingSettings.preserve_existing_schedule` keys off to
        # decide whether the existing slot layout is off-limits - see
        # that setting's docstring.
        self.schedule_pre_existed = schedule_pre_existed

    def legs(self):
        """Leg names of this panel: L1/L2 for split-phase, else R/Y/B."""
        return ("L1", "L2") if self.phase_config == SINGLE_PHASE else PHASES

    def get_layout(self):
        """The schedule's real layout, or the two-column assumption."""
        if self.layout is not None:
            return self.layout
        return SlotLayout(labels=self.legs())

    def real_circuits(self):
        """Connected circuits only - excludes Spare/Space placeholders."""
        return [c for c in self.circuits if not c.is_placeholder()]

    def used_slot_count(self):
        """
        THIRTY-SECOND CORRECTION: total PHYSICAL slots consumed by the
        circuits on this panel. `occupied_slots`/`free_slot_count`
        used to count CircuitInfo OBJECTS, so a 3-pole breaker counted
        as one slot instead of three. On a panel with any multi-pole
        content that overstated free capacity by (poles - 1) per
        circuit, which fed straight into the Percentage-mode
        empty-slot estimate in `schedule_service.run_preview`.
        """
        return sum(max(1, int(c.poles or 1)) for c in self.circuits)

    def occupied_slots(self):
        return sum(
            max(1, int(c.poles or 1))
            for c in self.circuits if not c.is_space
        )

    def free_slot_count(self):
        return max(0, self.num_slots - self.used_slot_count())

    def __repr__(self):
        return "<Panel '{}' {} {}slots {}circuits>".format(
            self.name, self.phase_config, self.num_slots,
            len(self.circuits)
        )


class PhaseLoadResult(object):
    """Aggregated load on a single phase leg, before or after balancing."""

    __slots__ = ("phase", "load_va", "circuit_count")

    def __init__(self, phase, load_va=0.0, circuit_count=0):
        self.phase = phase
        self.load_va = load_va
        self.circuit_count = circuit_count


class BalancingResult(object):
    """
    Full result of running the load-balancing algorithm against one
    panel. Captures both the "before" and "after" state so the UI can
    render a before/after comparison and the report service can print
    it without re-deriving anything.
    """

    __slots__ = (
        "panel_name", "before_phase_loads", "after_phase_loads",
        "before_imbalance_pct", "after_imbalance_pct", "moved_circuits",
        "total_connected_load_va", "total_connected_load_w",
        "after_phase_watts", "misaligned_3pole_circuits",
        "schedule_preserved", "spare_load_allocation",
        "spare_load_written_va", "total_spare_load_va", "notes",
    )

    def __init__(self, panel_name):
        self.panel_name = panel_name
        # FIFTY-SECOND CORRECTION: why balancing stopped short (full panel,
        # no free way on a leg ...) - shown in the batch log / report.
        self.notes = []
        # TWENTY-SIXTH / TWENTY-SEVENTH CORRECTIONS
        self.schedule_preserved = False
        # {phase: va} - how much NEW spare load balancing decided to
        # book against each leg.
        self.spare_load_allocation = {}
        # {phase: va} - what was actually written onto spare slots
        # (can fall short of the allocation if a leg had no spare slot
        # available to carry it).
        self.spare_load_written_va = {}
        self.total_spare_load_va = 0.0
        self.before_phase_loads = {}   # phase -> PhaseLoadResult (VA)
        self.after_phase_loads = {}    # phase -> PhaseLoadResult (VA)
        self.before_imbalance_pct = 0.0
        self.after_imbalance_pct = 0.0
        self.moved_circuits = []       # list[(CircuitInfo, old_phases, new_phases)]
        self.total_connected_load_va = 0.0
        # TWENTY-FOURTH CORRECTION: the post-move R-Y-B verification
        # (execute_batch's TWENTY-FIRST CORRECTION) previously only
        # logged a warning to pyRevit's log panel - easy to miss, and
        # exactly the piece of information this tool's own users kept
        # being asked to dig up and never could confirm either way.
        # Now tracked here directly so it can be shown in the Step 5
        # results grid and summary - no log-digging required to know
        # whether every 3-pole circuit actually landed on R-Y-B.
        self.misaligned_3pole_circuits = []  # list[(description, actual_phases_str)]
        # TWENTIETH CORRECTION: Watts-based counterparts, reported
        # alongside the VA figures used for the actual balancing math
        # (see CircuitInfo.true_load_w's docstring for the VA-vs-Watts
        # distinction and why balancing itself stays VA-based).
        self.total_connected_load_w = 0.0
        self.after_phase_watts = {}    # phase -> PhaseLoadResult (W)


class WaysBracketRow(object):
    """
    One row of the standard panel-size ('ways') bracket table used to
    look up Spare/Space quotas from a panel's connected circuit count.
    Ported from a proven prior extension (SpareSpaceFiller) - see
    services/spare_space_service.get_spare_space_counts_from_ways.
    Plain mutable attributes (not __slots__) so a WPF DataGrid can bind
    and edit them directly via reflection.
    """
    def __init__(self, ways, spare, space):
        self.Ways = ways
        self.Spare = spare
        self.Space = space


DEFAULT_WAYS_TABLE = [
    WaysBracketRow(12, 2, 2), WaysBracketRow(18, 2, 2),
    WaysBracketRow(24, 3, 3), WaysBracketRow(30, 3, 3),
    WaysBracketRow(36, 4, 4), WaysBracketRow(42, 5, 5),
    WaysBracketRow(48, 5, 5), WaysBracketRow(54, 6, 6),
]


class PanelTypeConfigRow(object):
    """
    One row of the panel-name-prefix -> (spare breaker rating, poles)
    lookup table. Ported from SpareSpaceFiller's Type Config table
    (e.g. panels named "ACP-..." get 30A/2P spares by default).
    """
    def __init__(self, prefix, spare_rating, poles):
        self.Prefix = prefix
        self.SpareRating = spare_rating
        self.Poles = poles


DEFAULT_TYPE_CONFIG_TABLE = [
    PanelTypeConfigRow("PP", 20, 1),
    PanelTypeConfigRow("LP", 20, 1),
    PanelTypeConfigRow("ELP", 20, 1),
    PanelTypeConfigRow("UDB", 30, 1),
    PanelTypeConfigRow("ACP", 30, 2),
]
DEFAULT_FALLBACK_RATING = 20
DEFAULT_FALLBACK_POLES = 1

SPARE_SPACE_MODE_PERCENTAGE = "Percentage"
SPARE_SPACE_MODE_WAYS_TABLE = "WaysTable"


class SpareSpaceSettings(object):
    """User-configurable settings from the wizard's Step 3."""

    __slots__ = (
        "spare_percentage", "space_percentage", "reserved_slots",
        "auto_fill_remaining", "mode", "ways_table", "type_config_table",
        "spare_load_va_per_pole", "spare_diversity_factor",
    )

    def __init__(self, spare_percentage=10.0, space_percentage=10.0,
                 reserved_slots=0, auto_fill_remaining=True,
                 mode=SPARE_SPACE_MODE_WAYS_TABLE, ways_table=None,
                 type_config_table=None, spare_load_va_per_pole=0.0,
                 spare_diversity_factor=1.0):
        # Assumed load to book against each spare POLE, in VA. 0.0
        # (the default) means "derive it from the spare's breaker
        # rating and the panel's line-to-neutral voltage"
        # (VA = rating_amps x voltage), which is what most spec
        # sections assume when they ask for spare capacity to be
        # shown on the schedule. Set a positive value to override
        # with a flat figure per pole.
        self.spare_load_va_per_pole = spare_load_va_per_pole
        # Diversity/demand factor applied to the derived spare load
        # (e.g. 0.5 books each spare at half its breaker capacity).
        # Applied to the derived figure AND to an explicit
        # `spare_load_va_per_pole`, so one knob covers both.
        self.spare_diversity_factor = spare_diversity_factor
        self.spare_percentage = spare_percentage
        self.space_percentage = space_percentage
        self.reserved_slots = reserved_slots
        self.auto_fill_remaining = auto_fill_remaining
        # "WaysTable" (default - matches the proven prior extension's
        # standard-panel-size bracket lookup) or "Percentage" (simpler,
        # percentage-of-total-slots mode kept for panels/firms that
        # don't use a fixed set of standard panel sizes).
        self.mode = mode
        self.ways_table = ways_table if ways_table is not None else list(DEFAULT_WAYS_TABLE)
        self.type_config_table = type_config_table if type_config_table is not None \
            else list(DEFAULT_TYPE_CONFIG_TABLE)


class BalancingSettings(object):
    """User-configurable settings from the wizard's Step 2."""

    __slots__ = (
        "enabled", "max_imbalance_pct", "respect_locked_circuits",
        "arrange_ryb_after_balance", "preserve_existing_schedule",
        "balance_via_spare_loads",
    )

    def __init__(self, enabled=True, max_imbalance_pct=5.0,
                 respect_locked_circuits=True,
                 arrange_ryb_after_balance=True,
                 preserve_existing_schedule=False,
                 balance_via_spare_loads=True):
        self.enabled = enabled
        self.max_imbalance_pct = max_imbalance_pct
        self.respect_locked_circuits = respect_locked_circuits
        self.arrange_ryb_after_balance = arrange_ryb_after_balance
        # TWENTY-SIXTH CORRECTION: when True (the default), a panel
        # that ALREADY had a panel schedule before this run keeps its
        # existing circuit layout exactly as-is: no MoveSlotTo calls,
        # no capacity shrink, no re-numbering. Existing schedules are
        # coordinated drawings - once they are issued, silently
        # relocating real circuits invalidates every drawing, cable
        # schedule and label that references those circuit numbers,
        # and the moves were never reliable anyway (see
        # schedule_service._apply_slot_plan's TWELFTH/THIRTEENTH
        # corrections on MoveSlotTo gridlock, and the TWENTY-FIRST
        # correction's R-Y-B verification failures). Balancing on
        # those panels is achieved instead by allocating the NEW
        # spare loads per phase - see `balance_via_spare_loads`.
        # Panels whose schedule this tool creates fresh are still
        # arranged normally; there is nothing issued to protect.
        #
        # FORTY-SIXTH CORRECTION: this defaulted to True. On a project
        # where every panel already has a schedule (the normal state of
        # a live job - 476 of 476 panels in the log that prompted this)
        # that made load balancing a no-op for the WHOLE project: the
        # tool reported success and moved nothing. The default is now
        # False, i.e. "Enable automatic load balancing" really balances;
        # ticking "Preserve existing panel schedules" is the explicit
        # opt-out, and Preview says how many panels each mode applies to.
        self.preserve_existing_schedule = preserve_existing_schedule
        # TWENTY-SEVENTH CORRECTION: balance by distributing the
        # planned spare capacity across the phase legs (heaviest leg
        # gets the least new spare load, lightest gets the most) and
        # writing those figures onto the spare slots with
        # PanelScheduleView.SetSpareLoadValue. Previously spares were
        # placed with a pole count and a breaker rating but NO load at
        # all, so every spare read as 0 VA and the panel's reported
        # phase totals did not reflect the new spare/space layout.
        self.balance_via_spare_loads = balance_via_spare_loads


class SpareSpaceReport(object):
    __slots__ = ("panel_name", "spares_added", "spaces_added",
                 "reserved_slots", "remaining_free_slots")

    def __init__(self, panel_name, spares_added=0, spaces_added=0,
                 reserved_slots=0, remaining_free_slots=0):
        self.panel_name = panel_name
        self.spares_added = spares_added
        self.spaces_added = spaces_added
        self.reserved_slots = reserved_slots
        self.remaining_free_slots = remaining_free_slots

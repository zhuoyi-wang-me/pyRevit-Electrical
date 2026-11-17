# -*- coding: utf-8 -*-
"""
balancing_service.py
=====================
Pure-Python load balancing engine. Contains NO Revit API calls so it
can be unit tested with plain fixture data (see README "Testing
Strategy"). The service operates on `models.PanelInfo` /
`models.CircuitInfo` objects only.

Algorithm
---------
Branch circuits fall into two groups with respect to balancing:

1. Multi-pole circuits (2-pole or 3-pole). Their poles are physically
   tied together (a 3-pole breaker always spans R, Y and B; a 2-pole
   breaker on a 3-phase panel spans two adjacent legs). These circuits
   cannot be reassigned to a single phase - moving them means moving
   the whole tied block. We treat each multi-pole circuit's load as a
   FIXED contribution to every phase it touches and do not attempt to
   move them for balancing purposes (this matches real panel behavior
   and avoids breaking circuit integrity, per requirement 4 "Preserve
   circuit integrity").

2. Single-pole circuits. These are freely assignable to any one of
   R/Y/B. This is where the LPT balancing algorithm operates - BUT
   only on panels this tool is allowed to physically rearrange. See
   "Two balancing modes" below.

We use a greedy longest-processing-time (LPT) bin-packing heuristic:
    - Start each phase's running total at the fixed load contributed
      by multi-pole circuits.
    - Sort single-pole circuits by descending apparent load.
    - Assign each circuit, in order, to whichever phase currently has
      the lowest running total.
    - Locked circuits (user-pinned in the Preview step) are assigned
      first, to their EXISTING phase, and excluded from the free pool.

LPT is a well-known 4/3-approximation for makespan minimization on
identical machines, which is more than sufficient for 3-bin panel
balancing and keeps the implementation simple, deterministic and fast
enough to run interactively on panels with hundreds of circuits.

Two balancing modes (TWENTY-SIXTH CORRECTION)
---------------------------------------------
REARRANGE mode (panels whose schedule this tool creates fresh):
    the LPT result above is applied by physically moving circuits
    between slots.

PRESERVE mode (panels that ALREADY had a panel schedule, and
`BalancingSettings.preserve_existing_schedule` is on - the default):
    every existing circuit stays exactly where it is. Nothing is
    moved, renumbered, or shrunk. Balancing is achieved purely by
    deciding how much of the NEW spare capacity to book against each
    leg - the lightest leg gets the most spare load, the heaviest the
    least - via `allocate_spare_load`, which the schedule service then
    writes onto the actual spare slots with
    `PanelScheduleView.SetSpareLoadValue`.

    This is the electrically honest reading of "balance the panel
    without touching the issued schedule": you cannot change what the
    connected legs already carry without moving conductors, but you
    CAN steer where the remaining declared capacity is allowed to
    grow, which is what determines whether the panel stays balanced
    as the spares get used.
"""

from panelmgr.models import PHASES, SINGLE_PHASE, PhaseLoadResult, BalancingResult


def phase_set_for_panel(panel_info):
    """
    The phase legs a given panel actually has. Split-phase 120/240 V
    residential-style boards are modelled as two legs, L1/L2.
    """
    if panel_info.phase_config == SINGLE_PHASE:
        return ("L1", "L2")
    return PHASES


def _balanceable(circuits):
    """Real connected circuits only - Spare/Space carry no real load."""
    return [c for c in circuits if not c.is_placeholder()]


def _fixed_multipole_loads(circuits, phase_set):
    """
    Returns {phase: fixed_load_va} contributed by multi-pole circuits,
    counting each circuit's PER-LEG share (see
    `models.CircuitInfo.phase_load_va`'s TWENTY-FIFTH CORRECTION - this
    used to add the circuit's full total to every leg it touched,
    inflating 3-pole circuits threefold and corrupting the LPT
    heuristic's view of which leg was lightest).
    """
    totals = dict((p, 0.0) for p in phase_set)
    for c in circuits:
        if c.poles > 1 and c.phases:
            for ph in c.phases:
                if ph in totals:
                    totals[ph] += c.phase_load_va(ph)
    return totals


def compute_phase_loads(circuits, use_watts=False, phase_set=None):
    """
    Returns {phase: PhaseLoadResult} for the CURRENT phase assignment
    of the given circuits (used for the "before" snapshot, and for the
    "after" totals shown in reporting).

    `use_watts`: sums the Watts-based `true_load_w` instead of
    `apparent_load_va`, for REPORTING purposes only. The actual
    balancing/imbalance-percentage math always uses VA (the
    electrically correct basis for phase balancing, since it tracks
    current draw) regardless of this flag - see
    `models.CircuitInfo.true_load_w`'s docstring for the full
    VA-vs-Watts distinction.

    `phase_set`: TWENTY-EIGHTH CORRECTION - the legs to report on.
    This used to be hardcoded to PHASES ("R","Y","B"), which raised an
    outright `KeyError: 'L1'` and killed the whole panel the moment
    `balance_panel` assigned a split-phase panel's circuits to L1/L2 -
    so single-phase panels could never complete, they were simply
    caught by execute_batch's per-panel handler and reported FAILED.
    Any leg present on a circuit but missing from `phase_set` is now
    accumulated rather than crashing.
    """
    if phase_set is None:
        phase_set = PHASES
    totals = dict((p, 0.0) for p in phase_set)
    counts = dict((p, 0) for p in phase_set)
    for c in circuits:
        if c.is_placeholder():
            continue
        for ph in c.phases:
            if ph not in totals:
                totals[ph] = 0.0
                counts[ph] = 0
            totals[ph] += c.phase_load_w(ph) if use_watts else c.phase_load_va(ph)
            counts[ph] += 1
    return dict(
        (p, PhaseLoadResult(p, totals[p], counts[p])) for p in totals
    )


def compute_imbalance_percentage(phase_loads):
    """
    Imbalance % = (max_phase_load - min_phase_load) / average_phase_load * 100
    Returns 0.0 if there is no load at all (nothing to balance).
    """
    loads = [pl.load_va for pl in phase_loads.values()]
    if not loads:
        return 0.0
    avg = sum(loads) / float(len(loads))
    if avg <= 0:
        return 0.0
    return (max(loads) - min(loads)) / avg * 100.0


def allocate_spare_load(fixed_phase_loads, total_spare_va, phase_set):
    """
    TWENTY-SEVENTH CORRECTION. Splits `total_spare_va` of new spare
    capacity across the phase legs so the resulting totals
    (fixed + allocated) come out as level as possible, WITHOUT moving
    any existing circuit. This is the balancing mechanism used in
    PRESERVE mode.

    Classic water-filling: repeatedly top up the lightest leg(s) to
    the level of the next-lightest until the spare budget runs out. If
    the budget is large enough to level every leg, the remainder is
    split evenly.

    Returns {phase: va_to_add}. Never returns a negative allocation -
    you cannot un-book load that is already connected, so a leg that
    is already heavier than the final target simply gets nothing.
    """
    alloc = dict((p, 0.0) for p in phase_set)
    if total_spare_va <= 0 or not phase_set:
        return alloc

    levels = dict((p, float(fixed_phase_loads.get(p, 0.0))) for p in phase_set)
    remaining = float(total_spare_va)

    # Fill the lightest legs up to the next distinct level, repeatedly.
    while remaining > 1e-9:
        lowest = min(levels.values())
        at_lowest = [p for p in phase_set if levels[p] <= lowest + 1e-9]
        higher = [levels[p] for p in phase_set if levels[p] > lowest + 1e-9]
        if higher:
            step = min(higher) - lowest
            cost = step * len(at_lowest)
            if cost <= remaining:
                for p in at_lowest:
                    alloc[p] += step
                    levels[p] += step
                remaining -= cost
                continue
        # Either every leg is already level, or the budget can't reach
        # the next level - spread what's left evenly over the lightest.
        share = remaining / float(len(at_lowest))
        for p in at_lowest:
            alloc[p] += share
            levels[p] += share
        remaining = 0.0

    return alloc


def balance_panel(panel_info, settings, reserved_slots=None):
    """
    Balances `panel_info` across R/Y/B (or L1/L2 for split-phase
    panels) and returns a `BalancingResult`.

    FIFTY-SECOND CORRECTION: `reserved_slots` - the slots the spare /
    space step will fill afterwards. Balancing used to run BEFORE that
    block was reserved and planned moves into those very slots; the
    arrangement step then found no free slot left on the target leg and
    dropped the circuits onto whatever leg still had room ("no free slot
    left on phase Y - placed on slot 1 instead"), which in a simulation
    turned a 4% plan into a 154% result. Moves are now planned only into
    slots that will really be free. The reserved slots still count as
    physically free for SWAPS (a swap passes one circuit through a free
    slot, and the spares are placed after the moves).

    A panel with no physically free slot at all cannot swap anything
    (Revit's MoveSlotTo needs a free destination), so no swap is planned
    there and `result.notes` says why.

    In REARRANGE mode this mutates `circuit.phases` in place on the
    CircuitInfo objects that were reassigned. In PRESERVE mode (see
    the module docstring) NOTHING is mutated - the result carries a
    `spare_load_allocation` instead, and `moved_circuits` stays empty.
    Either way, no Revit call is made from here.
    """
    result = BalancingResult(panel_info.name)
    phase_set = phase_set_for_panel(panel_info)

    active_circuits = _balanceable(panel_info.circuits)
    result.total_connected_load_va = sum(
        c.apparent_load_va for c in active_circuits
    )
    result.total_connected_load_w = sum(
        c.true_load_w for c in active_circuits
    )

    result.before_phase_loads = compute_phase_loads(
        active_circuits, phase_set=phase_set
    )
    result.before_imbalance_pct = compute_imbalance_percentage(
        result.before_phase_loads
    )

    def _finish_without_moving():
        result.after_phase_loads = compute_phase_loads(
            active_circuits, phase_set=phase_set
        )
        result.after_phase_watts = compute_phase_loads(
            active_circuits, use_watts=True, phase_set=phase_set
        )
        result.after_imbalance_pct = compute_imbalance_percentage(
            result.after_phase_loads
        )
        return result

    if not settings.enabled:
        return _finish_without_moving()

    # ---- PRESERVE mode -------------------------------------------------
    # The panel already has an issued schedule and we are not allowed to
    # rearrange it. Report the (unchanged) connected picture; the spare
    # load allocation is computed later by `plan_spare_balance` once the
    # spare budget is known, since that depends on the spare/space counts
    # the schedule service works out per panel.
    if settings.preserve_existing_schedule and panel_info.schedule_pre_existed:
        result.schedule_preserved = True
        return _finish_without_moving()

    # ---- REARRANGE mode ------------------------------------------------
    running_totals = _fixed_multipole_loads(active_circuits, phase_set)

    single_pole = [c for c in active_circuits if c.poles == 1]

    locked_ids = set(
        id(c) for c in single_pole
        if c.locked and settings.respect_locked_circuits
    )
    locked = [c for c in single_pole if id(c) in locked_ids]
    free = [c for c in single_pole if id(c) not in locked_ids]

    for c in locked:
        # THIRTY-FIFTH CORRECTION: a locked circuit whose phase was
        # never discovered (empty `phases` - happens when the schedule
        # scan could not resolve its slot) fell into a gap here. `ph`
        # defaulted to phase_set[0], which IS in running_totals, so the
        # `c.phases = (ph,)` repair never ran; `phase_load_va` then
        # returned 0.0 for a circuit with no phases, so the circuit's
        # whole load was silently dropped from the running totals AND
        # it stayed phase-less for every later calculation. Pin it to a
        # concrete leg first, then count it.
        if not c.phases or c.phases[0] not in running_totals:
            c.phases = (phase_set[0],)
        ph = c.phases[0]
        running_totals[ph] += c.phase_load_va(ph)

    # FORTY-EIGHTH CORRECTION: balance with the FEWEST moves.
    #
    # This used to re-deal every free single-pole circuit from scratch
    # (largest load first onto the lightest leg), ignoring where each
    # circuit already was. The totals came out level, but nearly every
    # circuit changed leg - on a live board that means dozens of
    # MoveSlotTo calls where two or three would do, and on a board with
    # few free ways those moves gridlock, so nothing ends up balanced.
    # It also ignored `max_imbalance_pct` entirely: a panel already
    # inside the allowed imbalance was reshuffled anyway.
    #
    # Now: every circuit starts on the leg it is on. While the panel is
    # outside the allowed imbalance, take the single change that helps
    # most - move one circuit from the heaviest leg to the lightest, or
    # swap one circuit between them - and repeat. A panel already within
    # the limit is left untouched.
    original = dict((id(c), c.phases) for c in free)
    for c in sorted(free, key=lambda c: c.apparent_load_va, reverse=True):
        if c.phases and c.phases[0] in running_totals:
            running_totals[c.phases[0]] += c.apparent_load_va
        else:
            # leg unknown (no slot yet): start it on the lightest leg
            leg = min(phase_set, key=lambda p: running_totals[p])
            c.phases = (leg,)
            running_totals[leg] += c.apparent_load_va

    reserved = set(reserved_slots or [])
    free_ways = _free_ways_per_leg(panel_info, phase_set, reserved)
    physically_free = _free_ways_per_leg(panel_info, phase_set)
    # swaps need one free slot to pass a circuit through (reserved ones
    # are fine - they are still empty while the moves run)
    can_swap = physically_free is None or sum(physically_free.values()) > 0
    target_pct = max(0.0, float(settings.max_imbalance_pct or 0.0))

    def _pct(totals):
        values = list(totals.values())
        average = sum(values) / float(len(values)) if values else 0.0
        return (max(values) - min(values)) / average * 100.0 if average > 0 else 0.0

    for _step in range(4 * len(free) + 10):
        if _pct(running_totals) <= target_pct:
            break
        heavy = max(phase_set, key=lambda p: running_totals[p])
        light = min(phase_set, key=lambda p: running_totals[p])
        gap = running_totals[heavy] - running_totals[light]
        if gap <= 1e-6:
            break
        on_heavy = [c for c in free if c.phases[0] == heavy]
        on_light = [c for c in free if c.phases[0] == light]

        # (remaining gap, circuits touched, circuit to move, circuit coming back)
        best = None
        if free_ways is None or free_ways.get(light, 0) > 0:
            for c in on_heavy:
                load = c.apparent_load_va
                if 0 < load < gap:
                    option = (abs(gap - 2.0 * load), 1, c, None)
                    if best is None or option[:2] < best[:2]:
                        best = option
        for c in (on_heavy if can_swap else []):
            for other in on_light:
                delta = c.apparent_load_va - other.apparent_load_va
                if 0 < delta < gap:
                    option = (abs(gap - 2.0 * delta), 2, c, other)
                    if best is None or option[:2] < best[:2]:
                        best = option
        if best is None:
            if not can_swap and _pct(running_totals) > target_pct:
                result.notes.append(
                    "Panel is full - no free way to move circuits through, so "
                    "circuits cannot be swapped between phases. Free at least one "
                    "way (remove a spare / space or increase the panel size) and "
                    "run again.")
            elif free_ways is not None and _pct(running_totals) > target_pct \
                    and free_ways.get(light, 0) <= 0 and not on_light:
                result.notes.append(
                    "No free way left on phase {} for a circuit to move to.".format(light))
            break                      # nothing left that improves the balance

        _remaining, _touched, mover, partner = best
        mover.phases = (light,)
        running_totals[heavy] -= mover.apparent_load_va
        running_totals[light] += mover.apparent_load_va
        if partner is not None:
            partner.phases = (heavy,)
            running_totals[light] -= partner.apparent_load_va
            running_totals[heavy] += partner.apparent_load_va
        elif free_ways is not None:
            free_ways[light] -= 1
            free_ways[heavy] = free_ways.get(heavy, 0) + 1

    for c in free:
        if original[id(c)] != c.phases:
            result.moved_circuits.append((c, original[id(c)], c.phases))

    result.after_phase_loads = dict(
        (p, PhaseLoadResult(p, running_totals[p],
                            _count_on_phase(active_circuits, p)))
        for p in phase_set
    )
    result.after_phase_watts = compute_phase_loads(
        active_circuits, use_watts=True, phase_set=phase_set
    )
    result.after_imbalance_pct = compute_imbalance_percentage(
        result.after_phase_loads
    )
    return result


def plan_spare_balance(panel_info, result, spare_slot_phases, spare_load_per_pole):
    """
    TWENTY-SEVENTH CORRECTION. Works out the per-spare-slot load values
    to write, given:

        spare_slot_phases:   {slot_key: phase} for every spare slot
                             that was (or is about to be) placed.
        spare_load_per_pole: the assumed VA each spare pole represents
                             (see SpareSpaceSettings.spare_load_va_per_pole).

    Returns (per_slot_va, allocation) where `per_slot_va` is
    {slot_key: va} ready to hand to
    `revit_utils.set_spare_slot_load`, and `allocation` is the
    {phase: va} target this was derived from.

    The total budget is `len(spare_slot_phases) * spare_load_per_pole` -
    i.e. the spare capacity the panel is declaring anyway. Balancing
    decides only WHERE that declared capacity lands, never how much of
    it there is; the spare/space counts themselves stay exactly as the
    Ways table / percentage settings determined. That keeps this
    orthogonal to the spare/space planning and means turning balancing
    off changes only the distribution, not the totals.
    """
    phase_set = phase_set_for_panel(panel_info)
    fixed = dict(
        (p, pl.load_va) for p, pl in
        compute_phase_loads(
            _balanceable(panel_info.circuits), phase_set=phase_set
        ).items()
    )

    total_budget = len(spare_slot_phases) * float(spare_load_per_pole)
    # FIFTY-FIFTH CORRECTION: water-fill only over the legs that HAVE a
    # spare slot to carry the load. The share planned for a leg without
    # one was simply never written, so the booked total fell short and
    # the legs that did get load were no longer level.
    legs_with_spares = tuple(p for p in phase_set if p in set(spare_slot_phases.values()))
    allocation = allocate_spare_load(fixed, total_budget, legs_with_spares or phase_set)
    for p in phase_set:
        allocation.setdefault(p, 0.0)

    per_slot_va = {}
    for phase in phase_set:
        slots_here = [k for k, ph in spare_slot_phases.items() if ph == phase]
        if not slots_here:
            continue
        share = allocation.get(phase, 0.0) / float(len(slots_here))
        for k in slots_here:
            per_slot_va[k] = share

    # Any spare sitting on a leg outside `phase_set` (shouldn't happen,
    # but a schedule with an unexpected layout could produce one) still
    # gets its nominal per-pole figure rather than silently zero.
    for k in spare_slot_phases:
        if k not in per_slot_va:
            per_slot_va[k] = float(spare_load_per_pole)

    result.spare_load_allocation = allocation
    result.total_spare_load_va = total_budget
    return per_slot_va, allocation


def projected_phase_loads(panel_info, allocation):
    """
    {phase: PhaseLoadResult} for connected load PLUS the allocated
    spare load - i.e. what the panel's legs will read once the new
    spares are in and carrying their declared capacity. This is the
    number that should be checked against
    `BalancingSettings.max_imbalance_pct` in PRESERVE mode, since the
    connected-only figure cannot change without moving conductors.
    """
    phase_set = phase_set_for_panel(panel_info)
    base = compute_phase_loads(
        _balanceable(panel_info.circuits), phase_set=phase_set
    )
    out = {}
    for p in phase_set:
        pl = base.get(p)
        load = (pl.load_va if pl else 0.0) + allocation.get(p, 0.0)
        out[p] = PhaseLoadResult(p, load, pl.circuit_count if pl else 0)
    return out


def _free_ways_per_leg(panel_info, phase_set, reserved=None):
    """
    {leg: number of empty ways on that leg}, from the real schedule
    layout and the circuits' real slots. Returns None when that is not
    known (no schedule yet, or a circuit without a slot) - the caller
    then does not limit moves by free ways. `reserved` slots (kept for
    spare / space) do not count as free.
    """
    layout = getattr(panel_info, "layout", None)
    if layout is None or layout.slot_phase is None:
        return None
    occupied = set(reserved or [])
    for c in panel_info.circuits:
        if c.slot is None:
            return None
        occupied.update(layout.run(c.slot, c.poles))
    free = dict((p, 0) for p in phase_set)
    for slot in range(1, int(panel_info.num_slots or 0) + 1):
        leg = layout.phase(slot)
        if leg in free and slot not in occupied:
            free[leg] += 1
    return free


def _count_on_phase(circuits, phase):
    return len([c for c in circuits if phase in c.phases])


def balance_multiple_panels(panels, settings, progress_callback=None):
    """
    Runs `balance_panel` across a list of PanelInfo objects. Returns a
    dict of {panel.element_id: BalancingResult}. `progress_callback`,
    if provided, is called as `progress_callback(index, total, panel)`
    after each panel so the UI can update its progress bar.
    """
    results = {}
    total = len(panels)
    for i, panel in enumerate(panels):
        results[panel.element_id] = balance_panel(panel, settings)
        if progress_callback:
            progress_callback(i + 1, total, panel)
    return results

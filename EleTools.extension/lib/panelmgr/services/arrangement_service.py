# -*- coding: utf-8 -*-
"""
arrangement_service.py
=======================
After balancing has assigned each circuit to a phase (R/Y/B), this
service determines the physical SLOT ORDER of circuits inside the
panel so the schedule reads in a clean R-Y-B-R-Y-B... sequence, the
convention most reviewers and inspectors expect.

Panel schedule slot convention assumed here (matches the standard
Revit panel-schedule two-column layout):
    Left column:  odd slots 1, 3, 5, 7, ...
    Right column: even slots 2, 4, 6, 8, ...
Both columns cycle R, Y, B, R, Y, B... top to bottom, i.e.
    slot 1 -> R   slot 2 -> R
    slot 3 -> Y   slot 4 -> Y
    slot 5 -> B   slot 6 -> B
    slot 7 -> R   slot 8 -> R  ...
This mirrors the physical bus-bar phase order used by most panel
manufacturers (each side of the gutter alternates R/Y/B down the can).

This is pure Python / Revit-free logic that computes a target slot
plan. Applying that plan to the live Revit panel schedule (physically
moving circuits between slots via `revit_utils.swap_schedule_slots`,
the only mechanism confirmed to actually change a circuit's phase -
see schedule_service._apply_slot_plan's TWELFTH CORRECTION for why a
direct circuit-number label write is NOT sufficient) is done by the
batch orchestration in `schedule_service.py`, so this module stays
unit testable.
"""

from panelmgr.models import PHASES


def circuit_slot_range(start_slot, poles, layout=None):
    """
    FORTY-FIFTH CORRECTION: pass the panel's `SlotLayout` (see
    models.SlotLayout) and the slots are the start slot plus the slots
    physically BELOW it in the real schedule. Without a layout the old
    two-column step-of-2 assumption described below is used.
    """
    if layout is not None:
        return layout.run(start_slot, poles)
    return _assumed_slot_range(start_slot, poles)


def _assumed_slot_range(start_slot, poles):
    """
    Returns the list of slot numbers a circuit starting at `start_slot`
    with `poles` poles actually occupies, using the same step-of-2
    (same-column) convention as `_find_consecutive_run`'s EIGHTEENTH
    CORRECTION - e.g. a 3-pole circuit starting at slot 1 occupies
    [1, 3, 5]. For a single-pole circuit (poles=1) this is just
    [start_slot]. This is the SINGLE SOURCE OF TRUTH for how a
    circuit's pole count maps to slot numbers - every place that
    needs to know "which slots does this circuit occupy" (occupancy
    bookkeeping, phase derivation, the move resolver) calls this
    rather than re-deriving the stepping convention itself, so they
    can never drift apart the way the phase-alignment search and the
    occupancy tracking previously did.
    """
    return [start_slot + 2 * k for k in range(poles)]


def phase_for_slot(slot, layout=None):
    """Given a 1-based slot number, returns which phase leg it maps to:
    from the real schedule when a `layout` is given, otherwise under the
    alternating two-column R-Y-B convention described above."""
    if layout is not None:
        return layout.phase(slot)
    # Slots pair up as (1,2)->R, (3,4)->Y, (5,6)->B, (7,8)->R, ...
    pair_index = (slot - 1) // 2      # 0,0,1,1,2,2,3,3,...
    return PHASES[pair_index % 3]


def build_slot_plan(panel_info, reserved_slots=None):
    """
    Returns (plan, warnings):
        plan: dict {circuit.element_id: target_slot} for every circuit
            in `panel_info`, arranged so that:
            - 3-pole circuits occupy three consecutive slots, one per
              phase, honoring R-Y-B order (they always touch all three
              legs, so their "slot" is really the slot of their first
              pole; the following two poles occupy the next two slots
              by definition of a 3-pole breaker).
            - 2-pole circuits occupy two consecutive slots on their two
              assigned legs.
            - 1-pole circuits are slotted into the next available slot
              whose `phase_for_slot` matches the circuit's assigned
              phase.
            - No two circuits are ever assigned the same slot
              (duplicate assignment prevention, requirement 4).
            - Circuits explicitly `locked` keep their current slot and
              are excluded from re-planning; their slot is reserved
              (removed from the pool) before anything else is planned.
        warnings: list of human-readable strings for anything planned
            imperfectly - SEVENTEENTH CORRECTION: a multi-pole circuit
            that can't find a free run matching its exact phase pattern
            falls back to ANY free consecutive run, even if that means
            its poles do NOT land on R-Y-B in order. A prior revision's
            code comment claimed this would be "flagged via description
            note" but no such flag was ever actually implemented - a
            user reported 3-pole circuits losing their R-Y-B sequence
            during balancing with no visible explanation why. This is
            now a real, returned warning identifying exactly which
            circuit and what phase pattern it actually landed on,
            surfaced up through schedule_service to the batch log.

    Args:
        reserved_slots: TWENTY-THIRD CORRECTION - an optional set of
            slot numbers permanently OFF-LIMITS to real-circuit
            placement, used to reserve a trailing block for
            spare/space BEFORE arrangement runs (rather than letting
            spare/space fall wherever gaps happen to land afterward,
            which a user correctly pointed out doesn't reliably put
            them at the bottom). Treated exactly like a locked
            circuit's slot - removed from the free pool up front.

    Space/spare filling is handled separately by
    `spare_space_service.py` / `revit_utils.fill_spare_and_space` after
    this plan is applied, using the reserved block (or, in Percentage
    mode, whatever slots remain).
    """
    # FORTY-FIFTH CORRECTION: every slot -> leg and "slot below" lookup
    # in this function goes through the panel's layout (the real
    # schedule when there is one), not a formula.
    layout = panel_info.get_layout()
    legs = tuple(layout.labels)
    total_slots = panel_info.num_slots
    all_slots = [s for s in range(1, total_slots + 1)
                 if layout.phase(s) is not None]

    plan = {}
    warnings = []
    reserved = set(reserved_slots or [])

    # 1) Locked circuits keep their existing slot.
    # THIRTY-THIRD CORRECTION: reserve EVERY slot the locked circuit
    # physically occupies, not just its primary one. A locked 3-pole
    # circuit at slot 7 occupies 7, 9 and 11 (`circuit_slot_range`),
    # but only slot 7 used to leave the free pool - so slots 9 and 11
    # stayed available and were handed out to other circuits, putting
    # two breakers on the same physical position. `validate_no_duplicate_slots`
    # could not catch it either, because the plan only ever stores a
    # multi-pole circuit's PRIMARY slot, so the two entries never
    # looked like duplicates.
    for c in panel_info.circuits:
        if c.locked and c.slot:
            plan[c.element_id] = c.slot
            reserved.update(circuit_slot_range(c.slot, c.poles, layout))

    free_slots = [s for s in all_slots if s not in reserved]

    # 2) Multi-pole circuits first (harder to place - need N
    #    consecutive free slots whose phase pattern matches R-Y-B or
    #    the appropriate 2-leg pair).
    multi_pole = [c for c in panel_info.circuits
                  if c.poles > 1 and c.element_id not in plan]
    # Largest pole-count first so 3-pole circuits get first pick of
    # clean triplets.
    multi_pole.sort(key=lambda c: c.poles, reverse=True)

    for c in multi_pole:
        # A circuit already sitting on a correctly phased run stays
        # where it is: moving a 3-pole breaker gains nothing for the
        # balance and every avoidable move is a risk on a live schedule.
        if c.slot and _run_is_usable(c.slot, c.poles, c.phases, free_slots, layout):
            for slot in circuit_slot_range(c.slot, c.poles, layout):
                free_slots.remove(slot)
            plan[c.element_id] = c.slot
            continue
        start_slot = _find_consecutive_run(free_slots, c.poles, c.phases, layout)
        phase_aligned = start_slot is not None
        if start_slot is None:
            # Fall back to any consecutive run, even if the phase
            # pattern isn't perfectly aligned; better to place the
            # circuit than to drop it entirely.
            start_slot = _find_any_consecutive_run(free_slots, c.poles, layout)
        if start_slot is None:
            warnings.append(
                "Circuit '{}' ({}-pole): no free consecutive run of {} "
                "slots available anywhere in the panel - it was left "
                "unslotted and remains on its original phase.".format(
                    c.description or c.circuit_number, c.poles, c.poles)
            )
            continue
        run = circuit_slot_range(start_slot, c.poles, layout)
        if not phase_aligned:
            actual_phases = tuple(phase_for_slot(s, layout) for s in run)
            warnings.append(
                "Circuit '{}' ({}-pole): no free run matched its "
                "required phase pattern {} - placed on slots {}-{} "
                "instead, which read as {} (NOT proper R-Y-B order). "
                "Manually verify this circuit's phase sequence.".format(
                    c.description or c.circuit_number, c.poles,
                    "-".join(c.phases), run[0], run[-1],
                    "-".join(str(p) for p in actual_phases)
                )
            )
        for slot in run:
            free_slots.remove(slot)
        plan[c.element_id] = start_slot
        if not phase_aligned:
            # Keep the in-memory phase assignment consistent with where
            # the circuit actually landed, so the reported "after"
            # loads describe the real layout rather than the one
            # balancing hoped for.
            c.phases = tuple(phase_for_slot(s, layout) for s in run)

    # 3) Single-pole circuits: bucket free slots by phase, fill in
    #    ascending slot order for a clean top-to-bottom read.
    slots_by_phase = {}
    for s in free_slots:
        slots_by_phase.setdefault(phase_for_slot(s, layout), []).append(s)
    for ph in slots_by_phase:
        slots_by_phase[ph].sort()

    single_pole = [c for c in panel_info.circuits
                   if c.poles == 1 and c.element_id not in plan]
    # FORTY-SEVENTH CORRECTION: a circuit that is ALREADY on the leg
    # balancing chose for it keeps its slot. This used to re-deal every
    # single-pole circuit into the first free slot of its leg in
    # circuit-number order, so a panel that needed two circuits swapped
    # had nearly all of them queued for MoveSlotTo - on a full board
    # that gridlocks (no free slot to move through), which is exactly
    # the "could not be moved, remains on its original phase" outcome.
    # Only circuits whose leg really changes are moved now.
    for c in single_pole:
        if c.slot and c.phases and phase_for_slot(c.slot, layout) == c.phases[0]:
            bucket = slots_by_phase.get(c.phases[0], [])
            if c.slot in bucket:
                bucket.remove(c.slot)
                plan[c.element_id] = c.slot
    single_pole = [c for c in single_pole if c.element_id not in plan]
    # Stable, deterministic order (by current circuit number, then id)
    single_pole.sort(key=lambda c: (c.circuit_number or "", c.element_id))

    for c in single_pole:
        ph = c.phases[0] if c.phases else legs[0]
        bucket = slots_by_phase.get(ph, [])
        borrowed_from = None
        if not bucket:
            # No slot left on the exact phase - borrow from the
            # alternative bucket with the MOST slots still free (the
            # comment here claimed "least-loaded" but the code simply
            # took the first non-empty bucket in R-Y-B order, which
            # piled every overflow circuit onto R).
            alternatives = [
                (alt, slots) for alt, slots in slots_by_phase.items()
                if alt != ph and slots
            ]
            if alternatives:
                alternatives.sort(key=lambda item: (-len(item[1]), item[0]))
                borrowed_from, bucket = alternatives[0]
        if not bucket:
            continue  # panel is genuinely full
        slot = bucket.pop(0)
        plan[c.element_id] = slot
        if borrowed_from is not None:
            # THIRTY-FOURTH CORRECTION: a borrowed slot puts the
            # circuit on a DIFFERENT leg than balancing assigned it
            # to. The circuit's `phases` used to be left pointing at
            # the intended leg, so every downstream "after" figure
            # (and the imbalance percentage derived from it) described
            # a distribution the panel does not actually have. Re-point
            # it at the real leg and say so.
            c.phases = (phase_for_slot(slot, layout),)
            warnings.append(
                "Circuit '{}' (1-pole): no free slot left on phase {} - "
                "placed on slot {} instead, which is phase {}. The "
                "balanced distribution could not be fully achieved on "
                "this panel.".format(
                    c.description or c.circuit_number, ph, slot,
                    phase_for_slot(slot, layout)
                )
            )

    return plan, warnings


def _run_matches(run, length, required_phases, layout):
    """Does this run of slots carry the phase pattern the circuit needs?"""
    run_phases = tuple(phase_for_slot(s, layout) for s in run)
    legs = tuple(layout.labels) if layout is not None else PHASES
    if length >= 3:
        return run_phases == legs[:length] and len(legs) >= 3
    if length == 2 and required_phases:
        return set(run_phases) == set(required_phases)
    return False


def _run_is_usable(start, length, required_phases, free_slots, layout):
    """True if the run starting at `start` is free and correctly phased."""
    run = layout.run(start, length, strict=True)
    if not run:
        return False
    free = set(free_slots)
    return all(s in free for s in run) and _run_matches(run, length, required_phases, layout)


def _find_consecutive_run(free_slots, length, required_phases, layout=None):
    """
    Looks for `length` slots ALL IN THE SAME COLUMN (same parity - left
    column = odd slot numbers, right column = even, per the module's
    2-column convention), stepping by 2 slots at a time so each step
    advances exactly one phase position (R -> Y -> B). Returns the
    starting slot, or None.

    EIGHTEENTH CORRECTION (significant): this used to search for
    `length` CONSECUTIVE INTEGERS (step of 1). Given `phase_for_slot`'s
    own 2-slots-per-phase pairing (slots 1,2->R; 3,4->Y; 5,6->B; ...,
    matching real printed schedules where CKT R1 and R2 sit on the
    same row and share a phase), 3 consecutive integers can NEVER span
    exactly (R, Y, B) - any 3-slot window under this pairing touches at
    most 2 distinct phases. This meant the phase-aligned path for
    3-pole circuits was silently impossible to satisfy, confirmed by a
    regression test failing even for a single 3-pole circuit in an
    otherwise completely empty 42-slot panel - every 3-pole circuit
    fell through to the misaligned fallback unconditionally. A 3-pole
    breaker physically spans 3 consecutive ROWS in ONE column (each
    row is a different phase), which in this slot-numbering convention
    means slots 2 apart (e.g. 1, 3, 5 = left column, rows 1-3 = R, Y,
    B) - not 3 raw consecutive slot numbers.
    """
    if layout is None:
        from panelmgr.models import SlotLayout
        layout = SlotLayout()
    free_sorted = sorted(free_slots)
    free_set = set(free_sorted)
    for start in free_sorted:
        run = layout.run(start, length, strict=True)
        if not run or not all(s in free_set for s in run):
            continue
        if _run_matches(run, length, required_phases, layout):
            return start
    return None


def _find_any_consecutive_run(free_slots, length, layout=None):
    """
    Same same-column, step-of-2 physical model as
    `_find_consecutive_run` (see EIGHTEENTH CORRECTION there), but
    without requiring a specific phase pattern - used as the fallback
    when no phase-aligned run exists anywhere.
    """
    if layout is None:
        from panelmgr.models import SlotLayout
        layout = SlotLayout()
    free_sorted = sorted(free_slots)
    free_set = set(free_sorted)
    for start in free_sorted:
        run = layout.run(start, length, strict=True)
        if run and all(s in free_set for s in run):
            return start
    return None


def trailing_free_slots(panel_info, count):
    """
    FIFTY-SECOND CORRECTION. The slots to keep for spare / space: the
    LAST `count` slots of the schedule that nothing occupies. This used
    to be simply the last `count` slot numbers - on a board whose bottom
    rows already held circuits, those circuits were thrown out of their
    slots to make room, and with nowhere left on their own phase they
    landed on another one, undoing the balance.
    """
    layout = panel_info.get_layout()
    occupied = set()
    for c in panel_info.circuits:
        if c.slot:
            occupied.update(circuit_slot_range(c.slot, c.poles, layout))
    out = []
    for slot in range(int(panel_info.num_slots or 0), 0, -1):
        if len(out) >= count:
            break
        if slot not in occupied and layout.phase(slot) is not None:
            out.append(slot)
    return set(out)


def validate_no_duplicate_slots(plan):
    """Sanity check used by the Preview step and by unit tests: raises
    ValueError if the same slot was assigned to two different
    circuits (requirement 4, 'Prevent duplicate circuit
    assignments')."""
    seen = {}
    for element_id, slot in plan.items():
        if slot in seen:
            raise ValueError(
                "Duplicate slot assignment: slot {} assigned to both "
                "circuit {} and circuit {}".format(
                    slot, seen[slot], element_id
                )
            )
        seen[slot] = element_id
    return True


def resolve_moves_with_staging(pending_moves, occupied_slots, total_slots, move_fn,
                               run_fn=None, final_positions=None):
    """
    Pure-Python, unit-testable multi-pass + staging-slot move resolver.
    Exists because `PanelScheduleView.MoveSlotTo` (the only confirmed
    way to physically move a circuit between slots) has no atomic
    swap - a destination must already be free. A single naive pass of
    direct moves gridlocks completely whenever every candidate
    destination is occupied by ANOTHER circuit that ALSO needs to
    move (exactly what happens on a nearly-fully-packed panel - a live
    comparison showed a panel's per-phase totals completely unchanged
    after a "successful" balance for exactly this reason).

    Args:
        pending_moves: dict {key: (current_slots, target_slots)} for
            every item that needs to move, where `current_slots` /
            `target_slots` are each an explicit LIST of every slot
            number that item occupies (e.g. `[7]` for a single-pole
            circuit, or `[1, 3, 5]` for a 3-pole circuit under
            `circuit_slot_range`'s step-of-2 same-column convention -
            NINETEENTH CORRECTION: earlier revisions passed a single
            "primary" slot plus a `poles` count and assumed a stepping
            pattern INSIDE this resolver, which duplicated (and, before
            the EIGHTEENTH CORRECTION, contradicted) the stepping
            convention `circuit_slot_range`/`phase_for_slot` define
            elsewhere. Taking explicit slot lists means this resolver
            makes NO assumption about how pole count maps to slot
            numbers - that responsibility lives in exactly one place
            (`circuit_slot_range`), so the two can never drift apart
            again.
        occupied_slots: set of slot numbers currently occupied by
            anything (every pending item's full slot list, plus
            anything else already fixed in place).
        total_slots: total slot count in the panel (used to find free
            slot RUNS for staging).
        move_fn: callable(from_primary_slot, to_primary_slot) -> bool;
            attempts the actual move (e.g. Revit's MoveSlotTo, given
            each list's first/primary slot) and reports success,
            assumed to relocate an entire multi-pole circuit as one
            atomic unit given its primary slot. Tests pass a fake that
            simulates occupancy without touching Revit, so this
            sequencing logic can be verified in isolation.

    Returns (resolved, unresolved):
        resolved: dict {key: final_primary_slot} for every item
            successfully moved to its target.
        unresolved: set of keys that could NOT be resolved (remained
            on their original slots) - either because the panel had no
            free slot RUN long enough anywhere to stage a
            deadlock-breaking move through, or because `move_fn`
            itself refused a move.

    Algorithm: each pass executes every pending move whose FULL target
    slot list is currently free (this pool grows as moves land). If a
    full pass makes zero progress (a cycle/gridlock), one pending item
    is staged into any genuinely free run of the same length and
    spacing as its own slot list to break the deadlock, and re-queued
    to reach its real target on a later pass. If no sufficient free run
    exists anywhere, resolution stops and remaining items are reported
    unresolved rather than looping forever.

    FIFTY-THIRD CORRECTION (from a live log: a 3-pole circuit and three
    single-pole circuits wanting each other's slots were all left
    "could not be moved"):
      - only the FIRST pending item was ever tried for staging; when that
        was a 3-pole circuit with no free 3-slot run, the resolver gave
        up although any single-pole circuit could have been staged
        through one free slot. Every pending item is now tried,
        single-pole first, and staging prefers slots no other pending
        move is waiting for.
      - an item that was staged but then never reached its target was
        reported as "remains on its original slot" while it really sat
        in the staging slot. It is now moved back to its original slot
        when that is free; if it cannot be, its real slot is written to
        `final_positions` ({key: primary slot}) when that dict is given.
    """
    pending = dict(pending_moves)  # key -> (current_slots, target_slots)
    original = dict((k, list(v[0])) for k, v in pending_moves.items())
    occupied = set(occupied_slots)
    resolved = {}
    staged_keys = set()

    def range_free(slots):
        return all(s not in occupied for s in slots)

    def mark(slots, add):
        for s in slots:
            if add:
                occupied.add(s)
            else:
                occupied.discard(s)

    def find_free_run(count, step, avoid=None):
        free_sorted = sorted(set(range(1, total_slots + 1)) - occupied)
        free_set = set(free_sorted)
        if avoid:
            # only a run no pending move is waiting for: parking on a slot
            # another circuit needs just blocks it (and costs real moves)
            allowed = free_set - set(avoid)
            return find_free_run_in(sorted(allowed), allowed, count, step)
        return find_free_run_in(free_sorted, free_set, count, step)

    def find_free_run_in(starts, free_set, count, step):
        for s in starts:
            # run_fn(start, count): the real "this slot and the ones
            # below it" from the schedule layout; the arithmetic step is
            # only the fallback when no layout is known.
            if run_fn is not None:
                candidate = run_fn(s, count)
                if not candidate:
                    continue
            else:
                candidate = [s + step * i for i in range(count)]
            if all(c in free_set for c in candidate):
                return candidate
        return None

    max_passes = len(pending) + 5  # a cycle of length N needs at most
    # N-1 direct moves plus one staging move to resolve.
    for _ in range(max_passes):
        if not pending:
            break

        progressed = False
        for key, (current_slots, target_slots) in list(pending.items()):
            if not range_free(target_slots):
                continue
            if move_fn(current_slots[0], target_slots[0]):
                mark(current_slots, add=False)
                mark(target_slots, add=True)
                resolved[key] = target_slots[0]
                del pending[key]
                progressed = True

        if not pending or progressed:
            continue

        # Zero progress this pass: every remaining move's destination
        # slots are occupied by another pending mover. Break the
        # deadlock by staging one item into any genuinely free run of
        # the same length/spacing as its own slot list - trying every
        # pending item, single-pole first (FIFTY-THIRD CORRECTION).
        wanted = set()
        for _k, (_cur, tgt) in pending.items():
            wanted.update(tgt)
        staged = False
        for key, (current_slots, target_slots) in sorted(
                pending.items(), key=lambda kv: (len(kv[1][0]), str(kv[0]))):
            if key in staged_keys:
                continue        # already parked - re-parking it only goes round in circles
            count = len(current_slots)
            step = (current_slots[1] - current_slots[0]) if count > 1 else 1
            staging_slots = find_free_run(count, step, avoid=wanted)
            if staging_slots is None:
                continue
            if not move_fn(current_slots[0], staging_slots[0]):
                continue
            mark(current_slots, add=False)
            mark(staging_slots, add=True)
            pending[key] = (staging_slots, target_slots)
            staged_keys.add(key)
            staged = True
            break
        if not staged:
            break  # true gridlock - nothing can be staged, give up cleanly

    # Items still pending: put a staged one back where it came from, so
    # "remains on its original slot" is true; otherwise report where it is.
    for key, (current_slots, _target) in list(pending.items()):
        home = original[key]
        if list(current_slots) == home:
            continue
        if range_free(home) and move_fn(current_slots[0], home[0]):
            mark(current_slots, add=False)
            mark(home, add=True)
        elif final_positions is not None:
            final_positions[key] = current_slots[0]

    unresolved = set(pending.keys())
    return resolved, unresolved

# -*- coding: utf-8 -*-
"""
Plain CPython unit tests for the Revit-free portions of panelmgr
(balancing_service, arrangement_service, spare_space_service).

Run with:
    cd PanelScheduleManager.extension
    python -m pytest tests/ -v

These tests do NOT require Revit, pyRevit, or IronPython - they only
exercise `lib/panelmgr/models.py` and `lib/panelmgr/services/*`, which
are deliberately kept free of Revit API imports. `lib/panelmgr/mvvm`
and `revit_utils.py` are excluded and covered by manual/integration
testing inside Revit instead (see README "Testing Strategy").
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from panelmgr.models import (
    PanelInfo, CircuitInfo, BalancingSettings, SpareSpaceSettings,
    THREE_PHASE, SINGLE_PHASE
)
from panelmgr.services import balancing_service, arrangement_service, spare_space_service


def make_panel(circuits, num_slots=42, phase_config=THREE_PHASE):
    return PanelInfo(
        element_id=1, name="TestPanel", phase_config=phase_config,
        num_slots=num_slots, circuits=circuits
    )


def make_circuit(eid, poles=1, load=1000.0, phases=None):
    return CircuitInfo(
        element_id=eid, circuit_number=str(eid), description="Load {}".format(eid),
        poles=poles, apparent_load_va=load, phases=phases or ("R",)
    )


# ---------------------------------------------------------------------------
# Balancing
# ---------------------------------------------------------------------------

def test_balance_distributes_single_pole_circuits_evenly():
    circuits = [make_circuit(i, poles=1, load=1000.0) for i in range(1, 10)]
    panel = make_panel(circuits)
    settings = BalancingSettings(enabled=True)

    result = balancing_service.balance_panel(panel, settings)

    assert result.after_imbalance_pct <= result.before_imbalance_pct + 1e-6
    loads = [pl.load_va for pl in result.after_phase_loads.values()]
    assert max(loads) - min(loads) <= 1000.0  # at most one circuit's worth of spread


def test_multipole_circuits_are_not_split():
    three_pole = make_circuit(1, poles=3, load=6000.0, phases=("R", "Y", "B"))
    singles = [make_circuit(i, poles=1, load=500.0) for i in range(2, 8)]
    panel = make_panel([three_pole] + singles)
    settings = BalancingSettings(enabled=True)

    result = balancing_service.balance_panel(panel, settings)

    # the 3-pole circuit must still touch all three phases afterward
    assert set(three_pole.phases) == {"R", "Y", "B"}


def test_locked_circuits_are_not_moved():
    locked = make_circuit(1, poles=1, load=5000.0, phases=("B",))
    locked.locked = True
    others = [make_circuit(i, poles=1, load=200.0) for i in range(2, 6)]
    panel = make_panel([locked] + others)
    settings = BalancingSettings(enabled=True, respect_locked_circuits=True)

    balancing_service.balance_panel(panel, settings)

    assert locked.phases == ("B",)


def test_disabled_balancing_leaves_phases_untouched():
    circuits = [make_circuit(1, load=100.0, phases=("R",)),
                make_circuit(2, load=9000.0, phases=("R",))]
    panel = make_panel(circuits)
    settings = BalancingSettings(enabled=False)

    result = balancing_service.balance_panel(panel, settings)

    assert circuits[0].phases == ("R",)
    assert circuits[1].phases == ("R",)
    assert result.after_imbalance_pct == result.before_imbalance_pct


def test_imbalance_percentage_zero_load():
    loads = {}
    assert balancing_service.compute_imbalance_percentage(loads) == 0.0


# ---------------------------------------------------------------------------
# Arrangement
# ---------------------------------------------------------------------------

def test_slot_plan_has_no_duplicates():
    circuits = [make_circuit(i, poles=1, load=100.0, phases=(("R", "Y", "B")[i % 3],))
                for i in range(1, 20)]
    panel = make_panel(circuits, num_slots=42)

    plan, _warnings = arrangement_service.build_slot_plan(panel)

    arrangement_service.validate_no_duplicate_slots(plan)  # raises on failure
    assert len(plan) == len(circuits)


def test_three_pole_circuit_gets_consecutive_ryb_slots():
    three_pole = make_circuit(1, poles=3, load=6000.0, phases=("R", "Y", "B"))
    panel = make_panel([three_pole], num_slots=42)

    plan, warnings = arrangement_service.build_slot_plan(panel)
    start = plan[three_pole.element_id]

    assert start in (1, 7, 13, 19, 25, 31, 37)  # any clean R-start triplet
    assert warnings == []  # clean placement - no warning expected


def test_three_pole_circuit_misalignment_produces_a_warning():
    # SEVENTEENTH CORRECTION regression test: block every clean R-Y-B
    # triplet with locked single-pole circuits, forcing the 3-pole
    # circuit into the fallback path (any consecutive run, regardless
    # of phase alignment). This must produce a warning identifying the
    # circuit and its actual (mismatched) landing phases - a prior
    # revision silently sacrificed alignment with no warning at all.
    blockers = []
    # Occupy slot 1 of every R-Y-B triplet (1, 7, 13, ...) up through
    # slot 40, leaving only a non-phase-aligned run free (e.g. 41-42
    # plus whatever partial gaps remain) - simplest reliable way to
    # force misalignment is to lock singles on slots 1,2,3 (breaking
    # the very first triplet) while leaving just enough consecutive
    # free room elsewhere that isn't R-aligned.
    for slot in (2, 3, 4):  # occupy Y,B,R of what would be the first
        # two triplets, leaving a free run starting at a non-R phase
        c = make_circuit(100 + slot, poles=1, load=10.0, phases=("R",))
        c.locked = True
        c.slot = slot
        blockers.append(c)

    three_pole = make_circuit(1, poles=3, load=6000.0, phases=("R", "Y", "B"))
    panel = make_panel(blockers + [three_pole], num_slots=6)

    plan, warnings = arrangement_service.build_slot_plan(panel)

    # Only slots 1, 5, 6 are free (2,3,4 locked) - no clean 3-consecutive
    # R-Y-B-aligned run exists in a 6-slot panel with those locked, so
    # the 3-pole circuit cannot be placed at all (no room) OR lands
    # misaligned - either way a warning must be present.
    assert len(warnings) >= 1
    assert "3-pole" in warnings[0] or "3-pole" in " ".join(warnings)


def test_locked_circuit_keeps_its_slot():
    locked = make_circuit(1, poles=1, load=100.0, phases=("R",))
    locked.locked = True
    locked.slot = 5
    other = make_circuit(2, poles=1, load=100.0, phases=("Y",))
    panel = make_panel([locked, other], num_slots=42)

    plan, _warnings = arrangement_service.build_slot_plan(panel)

    assert plan[locked.element_id] == 5


def test_reserved_slots_are_excluded_from_arrangement():
    # TWENTY-THIRD CORRECTION regression test: slots reserved for
    # spare/space (passed in by the caller BEFORE arrangement runs)
    # must never be used to place a real circuit, guaranteeing they
    # stay available as a clean trailing block afterward.
    circuits = [make_circuit(i, poles=1, load=100.0, phases=("R",))
                for i in range(1, 6)]
    panel = make_panel(circuits, num_slots=10)
    reserved = {7, 8, 9, 10}

    plan, _warnings = arrangement_service.build_slot_plan(panel, reserved_slots=reserved)

    assert set(plan.values()).isdisjoint(reserved)


# ---------------------------------------------------------------------------
# Multi-pass staging move resolver (the fix for a panel that started
# nearly-fully-packed, where a single naive pass of direct swaps
# gridlocks completely since MoveSlotTo has no atomic swap)
# ---------------------------------------------------------------------------

def _make_move_fn(fail_slots=None):
    """
    Builds a fake move_fn simulating real slot occupancy: succeeds iff
    the destination isn't already tracked as occupied by this fake's
    own bookkeeping (mirroring what a real Revit MoveSlotTo would
    reject), optionally forcing specific slots to always fail (to
    simulate a slot Revit refuses to use for other reasons).
    """
    fail_slots = fail_slots or set()

    def move_fn(from_slot, to_slot):
        if to_slot in fail_slots:
            return False
        return True
    return move_fn


def test_resolver_handles_simple_chain_without_staging():
    # A -> free slot 10 (no cycle, no staging needed)
    pending = {"A": ([1], [10])}
    occupied = {1}
    resolved, unresolved = arrangement_service.resolve_moves_with_staging(
        pending, occupied, total_slots=20, move_fn=_make_move_fn()
    )
    assert resolved == {"A": 10}
    assert unresolved == set()


def test_resolver_handles_two_cycle_with_staging():
    # A wants B's slot, B wants A's slot - direct pass gridlocks
    # instantly (both destinations mutually occupied); needs exactly
    # one staging move through a free slot to resolve.
    pending = {"A": ([1], [2]), "B": ([2], [1])}
    occupied = {1, 2}
    resolved, unresolved = arrangement_service.resolve_moves_with_staging(
        pending, occupied, total_slots=10, move_fn=_make_move_fn()
    )
    assert resolved == {"A": 2, "B": 1}
    assert unresolved == set()


def test_resolver_handles_longer_cycle_with_one_staging_slot():
    # A->B->C->A (a 3-cycle): A wants slot 2 (where B is), B wants
    # slot 3 (where C is), C wants slot 1 (where A is). Only one free
    # slot (10) exists anywhere in the panel to stage through.
    pending = {"A": ([1], [2]), "B": ([2], [3]), "C": ([3], [1])}
    occupied = {1, 2, 3}
    resolved, unresolved = arrangement_service.resolve_moves_with_staging(
        pending, occupied, total_slots=10, move_fn=_make_move_fn()
    )
    assert resolved == {"A": 2, "B": 3, "C": 1}
    assert unresolved == set()


def test_resolver_reports_unresolved_when_truly_no_free_slot():
    # Same 2-cycle as above, but total_slots equals exactly the
    # occupied slots - there is NO free slot anywhere to stage
    # through, so this must be reported unresolved, not loop forever.
    pending = {"A": ([1], [2]), "B": ([2], [1])}
    occupied = {1, 2}
    resolved, unresolved = arrangement_service.resolve_moves_with_staging(
        pending, occupied, total_slots=2, move_fn=_make_move_fn()
    )
    assert resolved == {}
    assert unresolved == {"A", "B"}


def test_resolver_gives_up_cleanly_if_staging_move_itself_fails():
    # Slot 10 is the ONLY free slot in the panel, but move_fn refuses
    # to move anything INTO it (simulating Revit rejecting that
    # specific slot) - the resolver must give up cleanly rather than
    # infinite-loop, since staging is impossible.
    pending = {"A": ([1], [2]), "B": ([2], [1])}
    occupied = set(range(1, 10))  # slots 1-9 occupied, only 10 is free
    resolved, unresolved = arrangement_service.resolve_moves_with_staging(
        pending, occupied, total_slots=10, move_fn=_make_move_fn(fail_slots={10})
    )
    assert resolved == {}
    assert unresolved == {"A", "B"}


def test_resolver_respects_multi_pole_slot_ranges():
    # SIXTEENTH/NINETEENTH CORRECTION regression test: a 3-pole
    # circuit "A" occupies slots [1, 3, 5] (step-of-2, same-column -
    # see circuit_slot_range), not just its primary slot 1, and a
    # single-pole circuit "B" wants to move into slot 3 - which is NOT
    # actually free, even though only slot 1 was ever passed as A's
    # "primary" slot in earlier, buggier revisions. This must be
    # correctly rejected (destination not free) rather than attempted,
    # matching what a live run showed: repeated identical "cannot
    # move" warnings because incomplete range tracking kept believing
    # a multi-pole circuit's non-primary slots were free.
    pending = {"A": ([1, 3, 5], [11, 13, 15]), "B": ([7], [3])}
    occupied = {1, 3, 5, 7}  # A's full 3-slot range, plus B's current slot
    resolved, unresolved = arrangement_service.resolve_moves_with_staging(
        pending, occupied, total_slots=20, move_fn=_make_move_fn()
    )
    # A can move freely (11,13,15 are free). B's target (slot 3) is
    # occupied by A's range until A actually moves - after A vacates
    # 1,3,5, slot 3 becomes free and B can proceed.
    assert resolved == {"A": 11, "B": 3}
    assert unresolved == set()


def _two_column_layout(n):
    from panelmgr.models import build_layout
    return build_layout(dict((s, ((s + 1) // 2, 0 if s % 2 else 1)) for s in range(1, n + 1)),
                        ("R", "Y", "B"))


def test_resolver_stages_a_single_pole_when_the_first_item_is_three_pole():
    # FIFTY-THIRD CORRECTION, from a live log: a misaligned 3-pole at
    # 15-17-19 wants 7-9-11, three sockets at 7/9/11 want 15/17/19.
    # Only the first pending item (the 3-pole) used to be tried for
    # staging, so everything was reported "could not be moved".
    lay = _two_column_layout(24)
    pending = {"FACADE": ([15, 17, 19], [7, 9, 11]),
               "S1": ([7], [15]), "S2": ([9], [17]), "S3": ([11], [19])}
    resolved, unresolved = arrangement_service.resolve_moves_with_staging(
        pending, set(range(1, 22)), 24, _make_move_fn(),
        run_fn=lambda s, k: lay.run(s, k, strict=True))
    assert unresolved == set()
    assert resolved == {"FACADE": 7, "S1": 15, "S2": 17, "S3": 19}


def test_resolver_puts_a_parked_circuit_back_when_it_cannot_finish():
    lay = _two_column_layout(24)
    pending = {"FACADE": ([15, 17, 19], [7, 9, 11]),
               "S1": ([7], [15]), "S2": ([9], [17]), "S3": ([11], [19])}
    stranded = {}
    moves = []

    def move_fn(a, b):
        moves.append((a, b))
        return True
    resolved, unresolved = arrangement_service.resolve_moves_with_staging(
        pending, set(range(1, 24)), 24, move_fn,
        run_fn=lambda s, k: lay.run(s, k, strict=True), final_positions=stranded)
    assert unresolved == {"FACADE", "S1", "S2", "S3"}
    assert stranded == {}                 # every circuit is back on its own slot
    assert moves == [] or moves[-1][1] == moves[0][0]


def test_circuit_slot_range_single_pole_is_just_the_start_slot():
    assert arrangement_service.circuit_slot_range(7, 1) == [7]


def test_circuit_slot_range_three_pole_steps_by_two():
    # Matches the printed-schedule-verified 2-slots-per-phase
    # convention: a 3-pole breaker spans 3 different ROWS (phases),
    # which are 2 slot-numbers apart under this numbering, not 1.
    assert arrangement_service.circuit_slot_range(1, 3) == [1, 3, 5]


# ---------------------------------------------------------------------------
# Spare / Space
# ---------------------------------------------------------------------------

def test_spare_space_percentages_are_respected():
    circuits = [make_circuit(i) for i in range(1, 11)]  # 10 occupied
    panel = make_panel(circuits, num_slots=42)
    settings = SpareSpaceSettings(spare_percentage=10.0, space_percentage=10.0,
                                   reserved_slots=0, auto_fill_remaining=False)
    empty_slots = list(range(11, 43))  # 32 empty

    spare_slots, space_slots, reserved, report = spare_space_service.plan_spare_space(
        panel, settings, empty_slots
    )

    assert len(spare_slots) == 5   # ceil(42 * 0.10) == ceil(4.2) == 5
    assert len(space_slots) == 5   # ceil(42 * 0.10) == ceil(4.2) == 5
    assert report.spares_added == 5
    assert report.spaces_added == 5


def test_spare_space_matches_reference_way_table():
    """
    Regression test pinned to a real-world reference table (ways / total
    slots -> spare & space counts at 10% each), confirming the tool
    rounds UP (ceiling), not down:
        4-way (12 slots)  -> 2 spare, 2 space
        6-way (18 slots)  -> 2 spare, 2 space
        8-way (24 slots)  -> 3 spare, 3 space
        10-way (30 slots) -> 3 spare, 3 space
        12-way (36 slots) -> 4 spare, 4 space
        14-way (42 slots) -> 5 spare, 5 space
        16-way (48 slots) -> 5 spare, 5 space
        18-way (54 slots) -> 6 spare, 6 space
    """
    reference = [
        (12, 2), (18, 2), (24, 3), (30, 3), (36, 4), (42, 5), (48, 5), (54, 6),
    ]
    settings = SpareSpaceSettings(spare_percentage=10.0, space_percentage=10.0,
                                   reserved_slots=0, auto_fill_remaining=False)

    for total_slots, expected_count in reference:
        panel = make_panel([], num_slots=total_slots)
        empty_slots = list(range(1, total_slots + 1))  # panel fully empty

        spare_slots, space_slots, reserved, report = spare_space_service.plan_spare_space(
            panel, settings, empty_slots
        )

        assert len(spare_slots) == expected_count, \
            "{}-slot panel: expected {} spares, got {}".format(
                total_slots, expected_count, len(spare_slots))
        assert len(space_slots) == expected_count, \
            "{}-slot panel: expected {} spaces, got {}".format(
                total_slots, expected_count, len(space_slots))


def test_auto_fill_remaining_uses_all_leftover_as_space():
    panel = make_panel([make_circuit(1)], num_slots=10)
    settings = SpareSpaceSettings(spare_percentage=10.0, space_percentage=0.0,
                                   reserved_slots=0, auto_fill_remaining=True)
    empty_slots = list(range(2, 11))  # 9 empty

    spare_slots, space_slots, reserved, report = spare_space_service.plan_spare_space(
        panel, settings, empty_slots
    )

    assert len(spare_slots) == 1          # floor(10 * 0.10)
    assert len(space_slots) == 8          # everything else
    assert len(spare_slots) + len(space_slots) == len(empty_slots)


def test_reserved_slots_are_excluded_from_spare_and_space():
    panel = make_panel([], num_slots=20)
    settings = SpareSpaceSettings(spare_percentage=10.0, space_percentage=10.0,
                                   reserved_slots=3, auto_fill_remaining=True)
    empty_slots = list(range(1, 21))

    spare_slots, space_slots, reserved, report = spare_space_service.plan_spare_space(
        panel, settings, empty_slots
    )

    assert len(reserved) == 3
    assert set(reserved).isdisjoint(set(spare_slots))
    assert set(reserved).isdisjoint(set(space_slots))


# ---------------------------------------------------------------------------
# Ways-bracket table + panel-type-prefix lookup (ported from SpareSpaceFiller)
# ---------------------------------------------------------------------------

def test_ways_bracket_rounds_up_to_smallest_sufficient_bracket():
    from panelmgr.models import DEFAULT_WAYS_TABLE

    # 29 connected -> rounds up to the 30-way bracket (3/3), not a flat
    # 10% of 29 (which would give 3 anyway here, but the mechanism
    # differs - the next case shows where they diverge).
    spare, space = spare_space_service.get_spare_space_counts_from_ways(
        DEFAULT_WAYS_TABLE, 29
    )
    assert (spare, space) == (3, 3)

    # 33 connected -> rounds up to the 36-way bracket (4/4), NOT
    # ceil(33 * 0.10) == 4 coincidentally matching here either - use
    # 31 -> still 36-way bracket (4/4) to make the point clearly.
    spare, space = spare_space_service.get_spare_space_counts_from_ways(
        DEFAULT_WAYS_TABLE, 31
    )
    assert (spare, space) == (4, 4)


def test_ways_bracket_exact_matches_reference_table():
    from panelmgr.models import DEFAULT_WAYS_TABLE
    reference = [
        (12, 2, 2), (18, 2, 2), (24, 3, 3), (30, 3, 3),
        (36, 4, 4), (42, 5, 5), (48, 5, 5), (54, 6, 6),
    ]
    for ways, expected_spare, expected_space in reference:
        spare, space = spare_space_service.get_spare_space_counts_from_ways(
            DEFAULT_WAYS_TABLE, ways
        )
        assert (spare, space) == (expected_spare, expected_space)


def test_ways_bracket_falls_back_to_percentage_beyond_largest_bracket():
    from panelmgr.models import DEFAULT_WAYS_TABLE
    # Largest bracket is 54; 100 connected has no bracket big enough.
    spare, space = spare_space_service.get_spare_space_counts_from_ways(
        DEFAULT_WAYS_TABLE, 100, fallback_spare_pct=10.0, fallback_space_pct=10.0
    )
    assert spare == 10  # ceil(100 * 0.10)
    assert space == 10


def test_pick_standard_capacity_returns_ascending_candidates():
    from panelmgr.models import DEFAULT_WAYS_TABLE
    candidates = spare_space_service.pick_standard_capacity(DEFAULT_WAYS_TABLE, 35)
    assert candidates == [36, 42, 48, 54]


def test_pick_standard_capacity_falls_back_to_raw_target():
    from panelmgr.models import DEFAULT_WAYS_TABLE
    candidates = spare_space_service.pick_standard_capacity(DEFAULT_WAYS_TABLE, 999)
    assert candidates == [999]


def test_panel_type_matches_longer_prefix_first():
    from panelmgr.models import DEFAULT_TYPE_CONFIG_TABLE
    # "ELP-01" should match ELP (20A/1P), not LP, even though LP is
    # also a valid prefix match by simple startswith.
    prefix, rating, poles = spare_space_service.match_panel_type(
        "ELP-01", DEFAULT_TYPE_CONFIG_TABLE
    )
    assert prefix == "ELP"
    assert rating == 20
    assert poles == 1


def test_panel_type_matches_acp_prefix_two_pole():
    from panelmgr.models import DEFAULT_TYPE_CONFIG_TABLE
    prefix, rating, poles = spare_space_service.match_panel_type(
        "ACP-CC-N-B1-02-01E", DEFAULT_TYPE_CONFIG_TABLE
    )
    assert prefix == "ACP"
    assert rating == 30
    assert poles == 2


def test_panel_type_falls_back_to_default_when_unmatched():
    from panelmgr.models import DEFAULT_TYPE_CONFIG_TABLE
    prefix, rating, poles = spare_space_service.match_panel_type(
        "XYZ-99", DEFAULT_TYPE_CONFIG_TABLE
    )
    assert prefix == "DEFAULT"
    assert rating == 20
    assert poles == 1

# -*- coding: utf-8 -*-
"""
Regression tests for the load-math and preserve-mode corrections
(TWENTY-FIFTH through TWENTY-NINTH). Pure Python - no Revit required.

Run with:  PYTHONPATH=lib python -m pytest tests/ -q
"""

import pytest

from panelmgr.models import (
    CircuitInfo, PanelInfo, BalancingSettings, SpareSpaceSettings,
    SINGLE_PHASE, THREE_PHASE,
)
from panelmgr.services import balancing_service as B


def circuit(eid, va, poles=1, phases=("R",), **kw):
    return CircuitInfo(element_id=eid, apparent_load_va=va, poles=poles,
                       phases=phases, **kw)


# ---------------------------------------------------------------------------
# TWENTY-FIFTH CORRECTION - multi-pole load is a per-leg share
# ---------------------------------------------------------------------------

def test_three_pole_load_splits_evenly_across_legs():
    c = circuit(1, 9000.0, poles=3, phases=("R", "Y", "B"))
    loads = B.compute_phase_loads([c])
    assert loads["R"].load_va == pytest.approx(3000.0)
    assert loads["Y"].load_va == pytest.approx(3000.0)
    assert loads["B"].load_va == pytest.approx(3000.0)
    total = sum(pl.load_va for pl in loads.values())
    assert total == pytest.approx(9000.0), "must not triple-count"


def test_two_pole_load_splits_across_its_two_legs():
    c = circuit(1, 4000.0, poles=2, phases=("R", "Y"))
    loads = B.compute_phase_loads([c])
    assert loads["R"].load_va == pytest.approx(2000.0)
    assert loads["Y"].load_va == pytest.approx(2000.0)
    assert loads["B"].load_va == pytest.approx(0.0)


def test_mixed_pole_panel_balances_on_true_per_leg_load():
    """
    With the old full-load-per-leg bug the 3-pole circuit looked like
    27 kVA spread as 9/9/9, which swamped the single-pole decisions.
    """
    panel = PanelInfo(element_id=1, name="LP-1", num_slots=42, circuits=[
        circuit(1, 9000.0, poles=3, phases=("R", "Y", "B"), slot=1),
        circuit(2, 3000.0, phases=("R",), slot=7),
        circuit(3, 3000.0, phases=("R",), slot=13),
        circuit(4, 3000.0, phases=("R",), slot=19),
    ])
    result = B.balance_panel(panel, BalancingSettings())
    legs = sorted(pl.load_va for pl in result.after_phase_loads.values())
    assert legs == pytest.approx([6000.0, 6000.0, 6000.0])
    assert result.after_imbalance_pct == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TWENTY-EIGHTH CORRECTION - split-phase panels no longer raise KeyError
# ---------------------------------------------------------------------------

def test_single_phase_panel_does_not_crash():
    panel = PanelInfo(element_id=2, name="SP-1", phase_config=SINGLE_PHASE,
                      num_slots=12, circuits=[
                          circuit(1, 4500.0, poles=2, phases=("L1", "L2")),
                          circuit(2, 800.0, phases=("L1",)),
                          circuit(3, 800.0, phases=("L2",)),
                      ])
    result = B.balance_panel(panel, BalancingSettings())
    assert set(result.after_phase_loads) == {"L1", "L2"}
    total = sum(pl.load_va for pl in result.after_phase_loads.values())
    assert total == pytest.approx(6100.0)


def test_single_phase_multipole_load_is_not_dropped():
    panel = PanelInfo(element_id=3, name="SP-2", phase_config=SINGLE_PHASE,
                      num_slots=12, circuits=[
                          circuit(1, 4500.0, poles=2, phases=("L1", "L2")),
                      ])
    result = B.balance_panel(panel, BalancingSettings())
    total = sum(pl.load_va for pl in result.after_phase_loads.values())
    assert total == pytest.approx(4500.0), "2-pole load vanished"


# ---------------------------------------------------------------------------
# TWENTY-NINTH CORRECTION - spares/spaces are not real load
# ---------------------------------------------------------------------------

def test_spare_and_space_excluded_from_phase_totals():
    circuits = [
        circuit(1, 1000.0, phases=("R",)),
        circuit(2, 9999.0, phases=("Y",), is_spare=True),
        circuit(3, 9999.0, phases=("B",), is_space=True),
    ]
    loads = B.compute_phase_loads(circuits)
    assert loads["R"].load_va == pytest.approx(1000.0)
    assert loads["Y"].load_va == pytest.approx(0.0)
    assert loads["B"].load_va == pytest.approx(0.0)


def test_real_circuits_filters_placeholders():
    panel = PanelInfo(element_id=4, name="LP-2", circuits=[
        circuit(1, 1000.0),
        circuit(2, 0.0, is_spare=True),
        circuit(3, 0.0, is_space=True),
    ])
    assert len(panel.real_circuits()) == 1


# ---------------------------------------------------------------------------
# TWENTY-SIXTH CORRECTION - preserve mode never moves a circuit
# ---------------------------------------------------------------------------

def _lopsided_panel(pre_existing):
    return PanelInfo(
        element_id=5, name="LP-3", num_slots=42,
        schedule_pre_existed=pre_existing,
        circuits=[
            circuit(1, 6000.0, phases=("R",), slot=1),
            circuit(2, 2000.0, phases=("Y",), slot=3),
            circuit(3, 1000.0, phases=("B",), slot=5),
        ],
    )


def test_preserve_mode_moves_nothing():
    panel = _lopsided_panel(pre_existing=True)
    before = [(c.element_id, c.phases, c.slot) for c in panel.circuits]
    # preserve is opt-in since the FORTY-SIXTH CORRECTION
    result = B.balance_panel(panel, BalancingSettings(preserve_existing_schedule=True))
    after = [(c.element_id, c.phases, c.slot) for c in panel.circuits]
    assert result.schedule_preserved is True
    assert result.moved_circuits == []
    assert before == after, "preserve mode must not mutate any circuit"


def test_new_schedule_still_gets_rearranged():
    # Everything starts stacked on R, so rearranging can genuinely help.
    # (Note the contrast with the preserve-mode fixture above: three
    # lumpy circuits on three legs are already as balanced as *moving*
    # can make them - which is exactly why spare-load allocation is the
    # useful lever on an existing board.)
    panel = PanelInfo(
        element_id=6, name="LP-4", num_slots=42, schedule_pre_existed=False,
        circuits=[circuit(i, 3000.0, phases=("R",), slot=1 + 2 * i)
                  for i in range(6)],
    )
    result = B.balance_panel(panel, BalancingSettings())
    assert result.schedule_preserved is False
    assert result.before_imbalance_pct == pytest.approx(300.0)
    assert result.after_imbalance_pct == pytest.approx(0.0)
    assert len(result.moved_circuits) == 4


def test_preserve_can_be_turned_off():
    panel = _lopsided_panel(pre_existing=True)
    settings = BalancingSettings(preserve_existing_schedule=False)
    result = B.balance_panel(panel, settings)
    assert result.schedule_preserved is False


# ---------------------------------------------------------------------------
# TWENTY-SEVENTH CORRECTION - spare load allocation
# ---------------------------------------------------------------------------

def test_allocate_spare_load_levels_the_legs():
    fixed = {"R": 7000.0, "Y": 4000.0, "B": 3000.0}
    alloc = B.allocate_spare_load(fixed, 14400.0, ("R", "Y", "B"))
    assert sum(alloc.values()) == pytest.approx(14400.0)
    finals = [fixed[p] + alloc[p] for p in ("R", "Y", "B")]
    assert max(finals) - min(finals) == pytest.approx(0.0, abs=1e-6)


def test_allocate_spare_load_never_goes_negative():
    """A leg already far heavier than the rest just gets nothing."""
    fixed = {"R": 50000.0, "Y": 1000.0, "B": 1000.0}
    alloc = B.allocate_spare_load(fixed, 2000.0, ("R", "Y", "B"))
    assert all(v >= 0 for v in alloc.values())
    assert alloc["R"] == pytest.approx(0.0)
    assert sum(alloc.values()) == pytest.approx(2000.0)


def test_allocate_spare_load_with_no_budget():
    alloc = B.allocate_spare_load({"R": 1.0, "Y": 2.0, "B": 3.0}, 0.0,
                                  ("R", "Y", "B"))
    assert all(v == 0.0 for v in alloc.values())


def test_plan_spare_balance_puts_most_load_on_lightest_leg():
    panel = _lopsided_panel(pre_existing=True)
    result = B.balance_panel(panel, BalancingSettings())
    spare_slots = {"a": "R", "b": "Y", "c": "B"}
    per_slot, alloc = B.plan_spare_balance(panel, result, spare_slots, 3000.0)
    assert result.total_spare_load_va == pytest.approx(9000.0)
    assert alloc["B"] > alloc["Y"] > alloc["R"]
    assert sum(per_slot.values()) == pytest.approx(9000.0)


def test_projected_loads_include_spare_allocation():
    panel = _lopsided_panel(pre_existing=True)
    result = B.balance_panel(panel, BalancingSettings())
    spare_slots = dict(("s%d" % i, p) for i, p in
                       enumerate(["R", "Y", "B", "R", "Y", "B"]))
    _per_slot, alloc = B.plan_spare_balance(panel, result, spare_slots, 2000.0)
    projected = B.projected_phase_loads(panel, alloc)
    pct = B.compute_imbalance_percentage(projected)
    assert pct < result.after_imbalance_pct, (
        "booking spare capacity on the light legs should improve the "
        "projected imbalance without moving anything"
    )


def test_spare_balance_does_not_change_the_spare_budget():
    """Balancing decides WHERE the spare capacity lands, not how much."""
    panel = _lopsided_panel(pre_existing=True)
    result = B.balance_panel(panel, BalancingSettings())
    spare_slots = {"a": "R", "b": "Y", "c": "B", "d": "R"}
    per_slot, _alloc = B.plan_spare_balance(panel, result, spare_slots, 2500.0)
    assert sum(per_slot.values()) == pytest.approx(4 * 2500.0)


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------

def test_defaults_are_conservative():
    b = BalancingSettings()
    # FORTY-SIXTH CORRECTION: preserving existing schedules is opt-in -
    # as the default it switched balancing off on every existing panel.
    assert b.preserve_existing_schedule is False
    assert b.balance_via_spare_loads is True
    s = SpareSpaceSettings()
    assert s.spare_load_va_per_pole == 0.0   # derive from breaker rating
    assert s.spare_diversity_factor == 1.0


# ---------------------------------------------------------------------------
# FIFTY-SECOND CORRECTION - balancing knows the spare/space reservation,
# and a full board does not plan swaps it cannot carry out
# ---------------------------------------------------------------------------

def _two_column_panel(n, loads):
    """loads: [(slot, va)] single-pole circuits on a two-column schedule."""
    from panelmgr.models import build_layout
    from panelmgr.services import arrangement_service as A
    cells = dict((s, ((s + 1) // 2, 0 if s % 2 else 1)) for s in range(1, n + 1))
    layout = build_layout(cells, ("R", "Y", "B"))
    panel = PanelInfo(element_id=9, name="PP-1", num_slots=n, layout=layout,
                      circuits=[circuit(100 + s, va, phases=(layout.phase(s),), slot=s)
                                for s, va in loads])
    return panel, layout, A


def _apply(panel, layout, A, reserved):
    """plan + resolve moves with a fake MoveSlotTo, return real imbalance"""
    plan, _w = A.build_slot_plan(panel, reserved_slots=reserved)
    pending = {}
    occupied = set()
    for c in panel.circuits:
        occupied.update(layout.run(c.slot, c.poles))
        t = plan.get(c.element_id)
        if t is not None and t != c.slot:
            pending[c.element_id] = (layout.run(c.slot, 1), layout.run(t, 1))
    resolved, unresolved = A.resolve_moves_with_staging(
        pending, occupied, panel.num_slots, lambda a, b: True,
        run_fn=lambda s, k: layout.run(s, k, strict=True))
    for c in panel.circuits:
        if c.element_id in resolved:
            c.slot = resolved[c.element_id]
        c.phases = (layout.phase(c.slot),)
    return B.compute_imbalance_percentage(B.compute_phase_loads(panel.circuits)), unresolved


def test_reserved_spare_slots_do_not_undo_the_balance():
    # 30 circuits, the heavy ones all on R, 12 free ways of which the
    # spare/space step keeps 10: the plan must still be achievable.
    loads = [(s, 2000.0 if ((s + 1) // 2 - 1) % 3 == 0 else 400.0) for s in range(1, 31)]
    panel, layout, A = _two_column_panel(42, loads)
    reserved = A.trailing_free_slots(panel, 10)
    assert reserved == set(range(33, 43))
    result = B.balance_panel(panel, BalancingSettings(), reserved_slots=reserved)
    real_pct, unresolved = _apply(panel, layout, A, reserved)
    assert not unresolved
    # what was planned is what the panel really ends up with ...
    assert real_pct == pytest.approx(result.after_imbalance_pct, abs=0.01)
    # ... and that is a real improvement (the old order ended at 154%)
    assert real_pct < result.before_imbalance_pct / 5.0


def test_reservation_never_takes_an_occupied_slot():
    loads = [(s, 500.0) for s in list(range(1, 15)) + [23, 24]]
    panel, layout, A = _two_column_panel(24, loads)
    reserved = A.trailing_free_slots(panel, 6)
    assert not reserved & {23, 24}
    assert len(reserved) == 6


def test_full_board_plans_no_impossible_swap():
    loads = [(s, 2000.0 if ((s + 1) // 2 - 1) % 3 == 0 else 500.0) for s in range(1, 43)]
    panel, layout, A = _two_column_panel(42, loads)
    result = B.balance_panel(panel, BalancingSettings())
    assert result.moved_circuits == []
    assert result.notes and "full" in result.notes[0]


def test_one_free_way_is_enough_to_swap():
    loads = [(s, 2000.0 if ((s + 1) // 2 - 1) % 3 == 0 else 500.0) for s in range(1, 42)]
    panel, layout, A = _two_column_panel(42, loads)
    result = B.balance_panel(panel, BalancingSettings())
    real_pct, unresolved = _apply(panel, layout, A, set())
    assert not unresolved
    assert real_pct < 15.0 < result.before_imbalance_pct

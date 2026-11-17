# -*- coding: utf-8 -*-
"""
Regression tests for the THIRTY-SECOND through FORTY-SIXTH
corrections - the round of debugging that followed the 25-31 batch.

Each test below FAILS against the code as it stood before that round.
Pure Python; no Revit, no pyRevit, no IronPython required.

Run with:  PYTHONPATH=lib python -m pytest tests/ -q
"""

import pytest

from panelmgr.models import (
    CircuitInfo, PanelInfo, BalancingSettings, SpareSpaceSettings,
    SINGLE_PHASE, THREE_PHASE,
)
from panelmgr.services import balancing_service as B
from panelmgr.services import arrangement_service as A
from panelmgr.services import report_service as R
from panelmgr import config


def circuit(eid, va=1000.0, poles=1, phases=("R",), **kw):
    return CircuitInfo(element_id=eid, apparent_load_va=va, poles=poles,
                       phases=phases, **kw)


def panel(circuits, num_slots=42, phase_config=THREE_PHASE, **kw):
    return PanelInfo(element_id=1, name="LP-1", phase_config=phase_config,
                     num_slots=num_slots, circuits=circuits, **kw)


# ---------------------------------------------------------------------------
# THIRTY-SECOND - slot accounting counts POLES, not circuit objects
# ---------------------------------------------------------------------------

def test_used_slot_count_counts_every_pole():
    p = panel([circuit(1, poles=3, phases=("R", "Y", "B")),
               circuit(2, poles=2, phases=("R", "Y")),
               circuit(3, poles=1)])
    # Three circuit objects, but six physical slots.
    assert p.used_slot_count() == 6


def test_free_slot_count_is_not_inflated_by_multipole():
    p = panel([circuit(1, poles=3, phases=("R", "Y", "B"))], num_slots=12)
    # Was 11 (12 - one circuit object); a 3-pole breaker eats three slots.
    assert p.free_slot_count() == 9


# ---------------------------------------------------------------------------
# THIRTY-THIRD - a locked multi-pole circuit reserves ALL of its slots
# ---------------------------------------------------------------------------

def test_locked_multipole_reserves_its_whole_slot_range():
    locked = circuit(1, poles=3, phases=("R", "Y", "B"), slot=1, locked=True)
    others = [circuit(i, poles=1, phases=("Y",)) for i in range(2, 8)]
    plan, _warn = A.build_slot_plan(panel([locked] + others))

    assert plan[1] == 1
    # Slots 3 and 5 are the locked circuit's other two poles. Nothing
    # else may be planned onto them.
    assigned_elsewhere = [s for eid, s in plan.items() if eid != 1]
    assert 3 not in assigned_elsewhere
    assert 5 not in assigned_elsewhere


def test_locked_multipole_collision_is_a_real_overlap_not_just_a_duplicate():
    """
    The old bug could not be caught by validate_no_duplicate_slots,
    because the plan only stores a multi-pole circuit's PRIMARY slot.
    Check genuine physical overlap instead.
    """
    locked = circuit(1, poles=3, phases=("R", "Y", "B"), slot=1, locked=True)
    others = [circuit(i, poles=1, phases=("Y",)) for i in range(2, 10)]
    p = panel([locked] + others)
    plan, _warn = A.build_slot_plan(p)

    by_id = dict((c.element_id, c) for c in p.circuits)
    seen = {}
    for eid, start in plan.items():
        for s in A.circuit_slot_range(start, by_id[eid].poles):
            assert s not in seen, \
                "slot {} used by circuit {} and {}".format(s, seen.get(s), eid)
            seen[s] = eid


# ---------------------------------------------------------------------------
# THIRTY-FOURTH - a borrowed slot re-points the circuit's phase and warns
# ---------------------------------------------------------------------------

def test_borrowed_slot_updates_the_circuit_phase_and_warns():
    # Only two slots exist, both on phase R (slots 1 and 2). Three
    # circuits all want R, so one must borrow.
    circuits = [circuit(1, phases=("R",)), circuit(2, phases=("R",)),
                circuit(3, phases=("R",))]
    p = panel(circuits, num_slots=4)
    plan, warnings = A.build_slot_plan(p)

    for c in circuits:
        if c.element_id in plan:
            # Whatever slot it landed on, its phase must describe THAT slot.
            assert c.phases == (A.phase_for_slot(plan[c.element_id]),)
    assert any("no free slot left on phase" in w for w in warnings)


# ---------------------------------------------------------------------------
# THIRTY-FIFTH - a locked circuit with no known phase keeps its load
# ---------------------------------------------------------------------------

def test_locked_circuit_without_a_phase_still_contributes_its_load():
    c = circuit(1, va=6000.0, phases=(), locked=True)
    result = B.balance_panel(panel([c]), BalancingSettings())

    assert c.phases, "circuit should have been pinned to a concrete leg"
    total = sum(pl.load_va for pl in result.after_phase_loads.values())
    # Was 0.0 - the load vanished from the totals entirely.
    assert total == pytest.approx(6000.0)


# ---------------------------------------------------------------------------
# THIRTY-NINTH - split-phase panels report on L1/L2 only
# ---------------------------------------------------------------------------

def test_single_phase_panel_reports_exactly_two_legs():
    p = panel([circuit(1, va=1000.0, phases=("L1",)),
               circuit(2, va=1000.0, phases=("L2",))],
              phase_config=SINGLE_PHASE)
    result = B.balance_panel(p, BalancingSettings())

    assert set(result.after_phase_loads.keys()) == set(["L1", "L2"])
    assert result.after_imbalance_pct == pytest.approx(0.0)


def test_split_phase_is_not_diluted_by_stray_ryb_legs():
    """
    The old reconcile step stamped R/Y/B onto split-phase circuits.
    compute_phase_loads then returned five legs, two of them always
    zero, so a perfectly balanced board reported a large imbalance.
    """
    p = panel([circuit(1, va=1000.0, phases=("L1",)),
               circuit(2, va=1000.0, phases=("L2",))],
              phase_config=SINGLE_PHASE)
    loads = B.compute_phase_loads(
        p.circuits, phase_set=B.phase_set_for_panel(p)
    )
    assert len(loads) == 2
    assert B.compute_imbalance_percentage(loads) == pytest.approx(0.0)


def test_phase_set_for_split_phase_panel():
    assert B.phase_set_for_panel(
        panel([], phase_config=SINGLE_PHASE)
    ) == ("L1", "L2")


# ---------------------------------------------------------------------------
# THIRTY-SIXTH - every setting the UI exposes survives a save/load cycle
# ---------------------------------------------------------------------------

def test_all_balancing_settings_round_trip_through_config():
    original = BalancingSettings(
        enabled=False, max_imbalance_pct=7.5,
        respect_locked_circuits=False, arrange_ryb_after_balance=False,
        preserve_existing_schedule=False, balance_via_spare_loads=False,
    )
    data = config.settings_to_dict(original, SpareSpaceSettings())
    restored = config.balancing_settings_from_dict(data)

    for field in BalancingSettings.__slots__:
        assert getattr(restored, field) == getattr(original, field), field


def test_all_spare_space_settings_round_trip_through_config():
    original = SpareSpaceSettings(
        spare_percentage=15.0, space_percentage=20.0, reserved_slots=4,
        auto_fill_remaining=False, spare_load_va_per_pole=750.0,
        spare_diversity_factor=0.5,
    )
    data = config.settings_to_dict(BalancingSettings(), original)
    restored = config.spare_space_settings_from_dict(data)

    # These two were silently dropped on every save.
    assert restored.spare_load_va_per_pole == 750.0
    assert restored.spare_diversity_factor == 0.5
    assert restored.reserved_slots == 4
    assert restored.auto_fill_remaining is False


def test_load_settings_does_not_hand_back_the_module_defaults():
    first = config.load_settings()
    first["balancing"]["max_imbalance_pct"] = 99.0
    second = config.load_settings()
    assert second["balancing"]["max_imbalance_pct"] != 99.0


# ---------------------------------------------------------------------------
# FORTY-FIFTH - the HTML export escapes its values
# ---------------------------------------------------------------------------

def test_html_export_escapes_panel_names_and_error_text(tmp_path):
    rows = [{
        "Panel": "LP-1 & 2",
        "Status": "FAILED: <script>alert(1)</script>",
    }]
    out = str(tmp_path / "report.html")
    R.export_html(rows, out)
    html = open(out, encoding="utf-8").read()

    assert "LP-1 &amp; 2" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_csv_export_writes_a_readable_file(tmp_path):
    rows = [{"Panel": "LP-1", "Status": "OK"}]
    out = str(tmp_path / "report.csv")
    assert R.export_csv(rows, out) == out
    assert "LP-1" in open(out).read()


# ---------------------------------------------------------------------------
# FORTY-SIXTH - report headers map to legal, bindable property names
# ---------------------------------------------------------------------------

def test_every_report_header_yields_a_bindable_property_name():
    from panelmgr.services.report_service import result_property_name

    bal = B.balance_panel(panel([circuit(1, va=1000.0)]), BalancingSettings())
    rows = R.build_report_rows([bal], {})

    for header in rows[0]:
        name = result_property_name(header)
        assert name, header
        # A dot would make WPF read the name as a nested property PATH.
        assert "." not in name, header
        assert name.replace("_", "").isalnum(), header
        assert not name[0].isdigit(), header


def test_imbalance_including_spares_header_is_not_a_nested_path():
    from panelmgr.services.report_service import result_property_name
    assert result_property_name(
        "Imbalance Incl. Spares (%)"
    ) == "ImbalanceInclSparesPct"

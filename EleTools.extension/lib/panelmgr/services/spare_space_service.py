# -*- coding: utf-8 -*-
"""
spare_space_service.py
========================
Detects empty circuit slots and plans how to fill them with Spare and
Space markers according to user-defined percentages and reserved
slots (requirement 2).

Definitions used (standard electrical engineering convention):
    Spare  = a slot with a breaker installed but no load connected
             (ready for future use, already protected/labeled).
    Space  = a slot with NO breaker installed (blank/open, physical
             capacity for a future breaker only).
    Reserved slots = slots the user wants left completely untouched
             (e.g. explicitly held for a known future tenant fit-out;
             excluded from both spare and space counts).

Pure Python / Revit-free planning logic lives here; the actual
`PanelScheduleView.AddSpare` / `AddSpace` Revit API calls are issued by
`schedule_service.py` using the plan this module returns.
"""

import math
import re

from panelmgr.models import (
    SpareSpaceReport, DEFAULT_FALLBACK_RATING, DEFAULT_FALLBACK_POLES,
)


def plan_spare_space(panel_info, settings, empty_slots):
    """
    Given a panel and the list of currently empty slot numbers, decide
    how many of those slots become Spare, how many become Space, how
    many stay Reserved (untouched), and return:

        (spare_slots, space_slots, reserved_slots, report)

    Percentages in `settings` are expressed as a percentage of the
    panel's TOTAL slot count (`panel_info.num_slots`), which is the
    common convention in electrical spec sections (e.g. "provide a
    minimum of 20% spare capacity"). Counts are rounded UP (ceiling),
    not down, matching the standard spec-section convention verified
    against a reference table (e.g. a 42-slot/14-way panel at 10%
    spare yields 5, not 4: ceil(42 * 0.10) = ceil(4.2) = 5). Rounding
    down would under-deliver the minimum percentage the spec actually
    requires.
    """
    total_slots = panel_info.num_slots
    empty_slots = sorted(empty_slots)

    reserved_count = min(settings.reserved_slots, len(empty_slots))
    reserved_slots = empty_slots[:reserved_count]
    remaining = empty_slots[reserved_count:]

    spare_target = int(math.ceil(total_slots * (settings.spare_percentage / 100.0)))
    space_target = int(math.ceil(total_slots * (settings.space_percentage / 100.0)))

    spare_slots = remaining[:spare_target]
    remaining_after_spare = remaining[spare_target:]

    if settings.auto_fill_remaining:
        # Fill everything left over as Space (per requirement:
        # "Automatically fill remaining unused slots").
        space_slots = remaining_after_spare
    else:
        space_slots = remaining_after_spare[:space_target]

    report = SpareSpaceReport(
        panel_name=panel_info.name,
        spares_added=len(spare_slots),
        spaces_added=len(space_slots),
        reserved_slots=len(reserved_slots),
        remaining_free_slots=len(remaining_after_spare) - len(space_slots)
        if not settings.auto_fill_remaining else 0,
    )
    return spare_slots, space_slots, reserved_slots, report


def plan_all_panels(panels_with_empty_slots, settings):
    """
    Convenience batch wrapper.
    `panels_with_empty_slots`: list of (PanelInfo, [empty_slot,...])
    Returns dict {panel.element_id: (spare_slots, space_slots,
                                      reserved_slots, report)}
    """
    results = {}
    for panel_info, empty_slots in panels_with_empty_slots:
        results[panel_info.element_id] = plan_spare_space(
            panel_info, settings, empty_slots
        )
    return results


# ---------------------------------------------------------------------------
# Standard "ways" bracket + panel-type-prefix lookups
# ---------------------------------------------------------------------------
# Ported (logic verbatim, style adapted) from a proven prior pyRevit
# extension (SpareSpaceFiller). This is the WAYS_TABLE mode referenced
# by `models.SPARE_SPACE_MODE_WAYS_TABLE`: rather than taking a flat
# percentage of a panel's CURRENT slot count, real panels come in
# standard sizes (12/18/24/30/36/42/48/54-way, etc). Given the panel's
# actual CONNECTED circuit count, round UP to the smallest standard
# size big enough to hold it, and use that bracket's fixed Spare/Space
# quota. This also lets the raw target (connected + spare*poles +
# space*poles) drive an increase of the panel's own capacity - see
# revit_utils.fill_spare_and_space, which is the Revit-facing
# counterpart of these two functions.

def get_spare_space_counts_from_ways(ways_rows, connected_count,
                                      fallback_spare_pct=10.0,
                                      fallback_space_pct=10.0):
    """
    Given the standard-size bracket table and a panel's actual
    connected circuit count, returns (spare_needed, space_needed) from
    the smallest bracket that is >= connected_count. If connected_count
    exceeds every bracket in the table, falls back to a straight
    ceiling percentage of the connected count (defaults to 10%/10%,
    matching the prior extension's hardcoded fallback, but exposed
    here as parameters since our tool already makes those percentages
    user-configurable in Step 3).
    """
    sorted_rows = sorted(ways_rows, key=lambda r: int(r.Ways))
    for row in sorted_rows:
        if int(row.Ways) >= connected_count:
            return int(row.Spare), int(row.Space)
    spare = int(math.ceil(connected_count * (fallback_spare_pct / 100.0)))
    space = int(math.ceil(connected_count * (fallback_space_pct / 100.0)))
    return spare, space


def pick_standard_capacity(ways_rows, raw_target):
    """
    Returns the ascending list of standard bracket sizes (the `Ways`
    column) that are >= raw_target, so the caller (revit_utils
    .fill_spare_and_space) can try the smallest one first and step up
    to the next if fragmented free slots leave it short on genuinely
    consecutive room. If nothing in the table is big enough, returns
    [raw_target] itself as the sole candidate.
    """
    candidates = sorted(set(
        int(r.Ways) for r in ways_rows if int(r.Ways) >= raw_target
    ))
    return candidates if candidates else [raw_target]


def match_panel_type(panel_name, type_rows, fallback_rating=DEFAULT_FALLBACK_RATING,
                      fallback_poles=DEFAULT_FALLBACK_POLES):
    """
    Matches a panel's name against the Prefix column of the type
    config table: either an exact prefix match ("LP...") or prefix +
    separator + digit ("ELP-01", "ELP 3"). Longer prefixes are checked
    first so "ELP" matches before "LP" inside "ELP-01". Returns
    (matched_prefix, spare_rating, poles); falls back to
    ("DEFAULT", fallback_rating, fallback_poles) if nothing matches -
    used as the STARTING SUGGESTION only, since the UI lets the value
    be overridden per panel.
    """
    name = (panel_name or "").upper()
    candidates = sorted(type_rows, key=lambda r: len(r.Prefix), reverse=True)
    for row in candidates:
        prefix = row.Prefix.upper()
        if re.match(r"^{}[\s\-_]?\d".format(re.escape(prefix)), name) or name.startswith(prefix):
            return row.Prefix, int(row.SpareRating), int(row.Poles)
    return "DEFAULT", fallback_rating, fallback_poles

# -*- coding: utf-8 -*-
"""
schedule_service.py
=====================
Orchestrates the end-to-end batch process against live Revit data:

    for each selected panel (per requirement, this exact order):
        1. read panel + circuits            (revit_utils.build_panel_info)
        2. GENERATE the panel schedule first (revit_utils
           .create_or_get_panel_schedule_view), in its OWN transaction
           that commits immediately (TENTH CORRECTION - relies on
           Transaction.Commit()'s implicit regeneration rather than an
           explicit mid-transaction Regenerate() call, which was found
           to fail 100% of the time on new-schedule creation in at
           least one real project). Nothing else is computed or
           written until this commits and the schedule's real slot
           count is confirmed readable.
        3. THEN load-balance                (balancing_service)
        4. THEN plan R-Y-B slot arrangement   (arrangement_service)
        5. THEN apply the slot plan and fill spare/space, phase-aligned,
           with breaker rating, in a SECOND transaction (spare_space_service
           for the counts/rating lookup, revit_utils.fill_spare_and_space
           for the physical write)
    Both transactions for one panel run inside a single outer
    TransactionGroup so the whole batch is one Undo step - but see
    `execute_batch`'s per-panel try/except for why a single panel's
    failure does NOT roll back panels already completed earlier in the
    same batch. IMPORTANT: because schedule creation (step 2) now
    commits on its own, a panel that fails at any LATER step keeps its
    newly-created schedule even though the panel is reported as
    FAILED overall - only work after schedule creation is rolled back.
    This is intentional (the schedule itself is a useful partial
    result) but is a real behavior difference from a single-transaction
    design, and is called out again inline where it matters.

This module is intentionally the "glue" layer - it imports both
`revit_utils` (Revit API) and the pure-Python services, and is
exercised via manual/integration testing inside Revit rather than
pure unit tests (see README "Testing Strategy").
"""

from panelmgr import revit_utils
from panelmgr.services import balancing_service, arrangement_service, spare_space_service
from panelmgr.models import SpareSpaceReport, SPARE_SPACE_MODE_WAYS_TABLE
from panelmgr.logger import get_logger

logger = get_logger(__name__)


class BatchCancelled(Exception):
    """Raised internally when the user hits Cancel mid-batch."""
    pass


def gather_panel_infos(doc, panel_elements):
    """Reads PanelInfo for each selected Revit panel element."""
    infos = []
    for el in panel_elements:
        infos.append((el, revit_utils.build_panel_info(doc, el)))
    return infos


def run_preview(panel_infos, balancing_settings, spare_space_settings):
    """
    Runs the (Revit-free) balancing + arrangement + spare/space
    planning against in-memory PanelInfo copies WITHOUT touching
    Revit, so Step 4 (Preview) can show accurate before/after numbers
    with an instant Cancel / no partial writes. Note this intentionally
    skips the live "generate the panel schedule first" step that
    `execute_batch` performs (see that function's docstring for the
    full generate -> balance -> arrange -> spare/space sequence) since
    schedule generation is a Revit write and Preview must stay
    side-effect-free; the numbers shown here are what WOULD happen.

    In WaysTable mode (the default - see models.SPARE_SPACE_MODE_WAYS_TABLE),
    spare/space counts and the spare breaker rating/poles are looked up
    the same way the live Execute pass will (spare_space_service
    .get_spare_space_counts_from_ways / match_panel_type) using
    `len(panel_info.circuits)` as the "connected ways" estimate - which
    only reads real ElectricalSystems, so it naturally excludes any
    spare/space placeholders already in the schedule (those aren't
    ElectricalSystem elements). The live batch re-derives this count
    from the actual schedule via `revit_utils.count_connected_ways`
    for the final, authoritative number, so a preview count can differ
    slightly if the schedule already contains spare/space placeholders
    that this Revit-free estimate can't see (there is nothing to read
    them from without touching Revit).

    Returns a dict:
        {
          panel.element_id: {
              "panel_info": PanelInfo,          # unmodified copy
              "balancing_result": BalancingResult,
              "slot_plan": {element_id: slot},
              "spare_needed": int,
              "space_needed": int,
              "spare_rating": int,
              "poles": int,
              "matched_type_prefix": str,
              "spare_space_report": SpareSpaceReport,
          }
        }
    """
    preview = {}
    for _el, info in panel_infos:
        working = _clone_panel_info(info)

        # Same rule as execute_batch: a panel whose real phase loads
        # contradict the schedule layout is reported, not rearranged.
        layout_trusted = working.get_layout().trusted()
        # (balancing runs below, after the spare/space slots are set aside -
        # the same order as execute_batch, FIFTY-SECOND CORRECTION)

        # FORTY-FIRST CORRECTION: Preview ignored preserve mode
        # entirely. `balance_panel` correctly returned a PRESERVE
        # result (nothing moved) for a panel that already had a
        # schedule, but this function then went on to build and show a
        # full R-Y-B slot plan for it anyway - a rearrangement
        # `execute_batch` will never perform, because it gates
        # arrangement on exactly this flag. Preview promised moves that
        # Execute was designed not to make.
        preserve = bool(
            balancing_settings.preserve_existing_schedule
            and working.schedule_pre_existed
        ) or not layout_trusted

        connected = len(working.real_circuits())

        if spare_space_settings.mode == SPARE_SPACE_MODE_WAYS_TABLE:
            spare_needed, space_needed = spare_space_service.get_spare_space_counts_from_ways(
                spare_space_settings.ways_table, connected,
                spare_space_settings.spare_percentage,
                spare_space_settings.space_percentage,
            )
            matched_prefix, spare_rating, poles = spare_space_service.match_panel_type(
                working.name, spare_space_settings.type_config_table
            )
            ss_report = SpareSpaceReport(
                panel_name=working.name, spares_added=spare_needed,
                spaces_added=space_needed, reserved_slots=0,
                remaining_free_slots=0,
            )
            # Mirror execute_batch's TWENTY-THIRD CORRECTION: the
            # trailing block held for spare/space is reserved BEFORE
            # arrangement, so the preview's slot plan is the one that
            # will actually be applied rather than one computed against
            # a different free-slot pool.
            reserve_count = min(
                (spare_needed + space_needed) * poles, working.num_slots
            )
            reserved_for_spare_space = (
                set() if preserve
                else arrangement_service.trailing_free_slots(working, reserve_count))
        else:
            # Empty slots are the PHYSICAL slots left over, so a
            # multi-pole circuit has to count for all of its poles;
            # `len(real_circuits())` counted it once, overstating free
            # capacity on every panel with 2- or 3-pole breakers.
            used = working.used_slot_count()
            empty_slot_numbers = list(range(used + 1, working.num_slots + 1))
            spare_slots, space_slots, _reserved, ss_report = \
                spare_space_service.plan_spare_space(
                    working, spare_space_settings, empty_slot_numbers
                )
            spare_needed, space_needed = len(spare_slots), len(space_slots)
            matched_prefix, spare_rating, poles = "N/A", 20, 1
            reserved_for_spare_space = set()

        bal_result = balancing_service.balance_panel(
            working,
            balancing_settings if layout_trusted else _balancing_off(balancing_settings),
            reserved_slots=reserved_for_spare_space,
        )
        bal_result.schedule_preserved = preserve
        for note in bal_result.notes:
            logger.warning("Preview - panel '%s': %s", working.name, note)

        if balancing_settings.arrange_ryb_after_balance and not preserve:
            slot_plan, arrangement_warnings = arrangement_service.build_slot_plan(
                working, reserved_slots=reserved_for_spare_space
            )
            arrangement_service.validate_no_duplicate_slots(slot_plan)
            for w in arrangement_warnings:
                logger.warning("Preview - panel '%s': %s", working.name, w)
            # build_slot_plan can re-point a circuit's phase when it
            # could not honour the balanced assignment, so the "after"
            # figures are refreshed from the final state.
            _refresh_after_loads(working, bal_result)
        else:
            slot_plan = {}

        preview[info.element_id] = {
            "panel_info": working,
            "balancing_result": bal_result,
            "slot_plan": slot_plan,
            "spare_needed": spare_needed,
            "space_needed": space_needed,
            "spare_rating": spare_rating,
            "poles": poles,
            "matched_type_prefix": matched_prefix,
            "spare_space_report": ss_report,
            "preserve": preserve,
        }
    return preview


def _refresh_after_loads(panel_info, bal_result):
    """
    Recomputes a BalancingResult's "after" figures from the circuits'
    CURRENT phase assignment.

    FORTY-SECOND CORRECTION: `balance_panel` computes the after-state
    from its own running totals, i.e. from the distribution it INTENDED
    to produce. Two later stages can change that intent - arrangement
    borrowing a slot on another leg, and `_apply_slot_plan` failing to
    complete a move - and neither used to feed back into the result. A
    panel whose moves all gridlocked therefore reported a healthy
    balanced "after" figure while its schedule was untouched, which is
    precisely the false-success the TWELFTH and THIRTEENTH corrections
    were chasing. Calling this after those stages makes the reported
    numbers describe the panel that actually exists.
    """
    phase_set = balancing_service.phase_set_for_panel(panel_info)
    active = [c for c in panel_info.circuits if not c.is_placeholder()]
    bal_result.after_phase_loads = balancing_service.compute_phase_loads(
        active, phase_set=phase_set
    )
    bal_result.after_phase_watts = balancing_service.compute_phase_loads(
        active, use_watts=True, phase_set=phase_set
    )
    bal_result.after_imbalance_pct = balancing_service.compute_imbalance_percentage(
        bal_result.after_phase_loads
    )


def _clone_panel_info(panel_info):
    from panelmgr.models import PanelInfo
    clone = PanelInfo(
        element_id=panel_info.element_id,
        name=panel_info.name,
        phase_config=panel_info.phase_config,
        voltage=panel_info.voltage,
        num_slots=panel_info.num_slots,
        circuits=[c.clone() for c in panel_info.circuits],
        panel_schedule_view_id=panel_info.panel_schedule_view_id,
        # Must be carried across or Preview shows REARRANGE numbers for
        # a panel that Execute will actually PRESERVE - the two steps
        # would disagree on every existing panel.
        schedule_pre_existed=panel_info.schedule_pre_existed,
        # read-only once built, so Preview can share it
        layout=panel_info.layout,
    )
    return clone


def execute_batch(doc, panel_infos, balancing_settings, spare_space_settings,
                   progress_callback=None, cancel_check=None, template_id=None):
    """
    Executes the full write pass against Revit.

    Args:
        doc: Revit Document
        panel_infos: list of (panel_element, PanelInfo)
        progress_callback: callable(index, total, panel_name, message)
        cancel_check: callable() -> bool; if it returns True the batch
            stops and rolls back everything via TransactionGroup. A
            per-panel failure (not a cancellation) does NOT trigger
            this - it's isolated to that one panel; see
            `failed_panels` in the return value.
        template_id (int|None): ElementId.IntegerValue of a specific
            PanelScheduleTemplate to use for newly-created schedules
            (see revit_utils.get_panel_schedule_templates). Panels
            that already have a schedule are unaffected - the
            template only applies at creation time.

    Returns: list[BalancingResult], list[SpareSpaceReport], list[(panel_name, error_message)]
    Raises: BatchCancelled if the user cancelled.
    """
    total = len(panel_infos)
    balancing_results = []
    spare_space_reports = []
    failed_panels = []

    with revit_utils.RevitTransactionGroup(doc, "Panel Schedule Manager - Batch Update") as tg:
        for i, (panel_element, panel_info) in enumerate(panel_infos):
            if cancel_check and cancel_check():
                tg.rollback()
                raise BatchCancelled()

            panel_name = panel_info.name

            # Each panel's processing is isolated in its own try/except.
            # A failure here rolls back ONLY that panel's own
            # RevitTransaction (see revit_utils.RevitTransaction) and
            # is recorded in `failed_panels` - it must NOT propagate
            # out of this loop, because doing so would trip the outer
            # TransactionGroup's rollback and undo every panel already
            # successfully processed earlier in this same batch (a
            # live traceback confirmed this: one panel with no
            # compatible schedule template aborted the entire run,
            # discarding already-committed work for panels processed
            # before it). Only an explicit user Cancel should roll
            # back the whole group - see the cancel_check above.
            try:
                # Explicit pipeline order, per requirement: (1) generate
                # / ensure the panel schedule exists first, (2) THEN
                # load-balance against it, (3) THEN fill spare/space.
                #
                # TENTH CORRECTION: schedule creation now runs in its
                # OWN transaction that commits immediately, separate
                # from the balancing/arrangement/spare-space
                # transaction that follows. Previously everything ran
                # in one transaction with an explicit doc.Regenerate()
                # call right after creation - that call hit "Failed to
                # regenerate" on every single new-schedule-creation
                # attempt tested (100% reproducible across multiple
                # panel types), so it's been replaced with reliance on
                # Transaction.Commit()'s IMPLICIT regeneration instead,
                # a different Revit API code path. IMPORTANT TRADEOFF:
                # because schedule creation now commits on its own, a
                # panel that fails at the slot-count-reconciliation
                # step below (or later) will KEEP its newly-created
                # schedule even though the panel is reported as
                # FAILED - unlike before, where the whole panel's work
                # rolled back atomically. This is intentional: the
                # schedule itself is a legitimate, useful partial
                # result, and re-running this panel later will find it
                # via the "already exists" path rather than needing to
                # recreate it.
                _report_progress(progress_callback, i, total, panel_name,
                                  "Generating panel schedule...")

                with revit_utils.RevitTransaction(
                    doc, "Create Panel Schedule - {}".format(panel_name)
                ):
                    psv = revit_utils.create_or_get_panel_schedule_view(
                        doc, panel_element, panel_info, template_id=template_id
                    )
                # transaction commits here - implicit regenerate happens
                # as part of Commit(), not via an explicit mid-transaction
                # Regenerate() call (see TENTH CORRECTION above).

                # Reconcile against the schedule's REAL slot count now
                # that it definitely exists (it may have just been
                # created, in which case this is the first time we can
                # know the true value - see revit_utils module
                # docstring, THIRD CORRECTION). Doing this before
                # arrangement/spare-space planning is what prevents
                # "row number nRow is invalid in Body" failures on
                # panels whose schedule template has fewer slots than
                # the family's nominal "Slots" parameter claims. This
                # read happens OUTSIDE any transaction - safe, since
                # GetTableData()/GetSectionData() are read-only calls
                # (the same pattern already used for existing schedules
                # at Step 1 discovery time).
                actual_slots = revit_utils.get_total_slots(psv, doc=doc)
                if actual_slots and actual_slots != panel_info.num_slots:
                    logger.warning(
                        "Panel '%s': clamping nominal slot count (%s) to "
                        "actual schedule slot count (%s).",
                        panel_name, panel_info.num_slots, actual_slots
                    )
                    panel_info.num_slots = actual_slots
                elif not actual_slots:
                    # FIFTH CORRECTION (severe): a live run crashed
                    # Revit itself even AFTER the Regenerate()-retry
                    # was removed (see FOURTH CORRECTION). The
                    # crash happened immediately after these exact
                    # "0 slots" warnings, with no further Python
                    # traceback possible (a native crash, not a
                    # catchable exception) - strong evidence that
                    # the earlier version's choice to proceed
                    # anyway with a nominal/guessed slot count, and
                    # keep calling MORE Revit API methods
                    # (GetCellsBySlotNumber, GetCircuitByCell,
                    # AddSpare, MoveSlotTo, etc.) against a
                    # PanelScheduleView that has already failed to
                    # answer basic read calls, is itself what
                    # destabilized Revit. A panel schedule that
                    # can't even report its own slot count is
                    # signaling a genuinely broken/invalid
                    # underlying object - the safe response is to
                    # stop touching it immediately, not to guess
                    # and continue. Raising here is caught by this
                    # panel's own try/except below: the schedule
                    # created just above is KEPT (see TENTH
                    # CORRECTION), but no further changes are made
                    # to it, and the rest of the batch continues
                    # untouched.
                    raise RuntimeError(
                        "The panel schedule for '{}' was created, but its "
                        "slot count could not be read even after every "
                        "fallback - Revit's own implicit regeneration on "
                        "transaction commit did not resolve it either. "
                        "This strongly suggests Revit cannot fully "
                        "regenerate a NEW panel schedule in this document "
                        "right now, independent of this tool. Try creating "
                        "one manually via Revit's own UI (Systems tab -> "
                        "Panel Schedules -> New) for this exact panel to "
                        "confirm; if that also fails, this needs a "
                        "Revit-side Audit (File -> Open -> check Audit), "
                        "not a code fix. The schedule that was already "
                        "created has been kept.".format(panel_name)
                    )

                # TWELFTH CORRECTION + FOURTEENTH CORRECTION: populate
                # each circuit's REAL current slot, and recover any
                # circuit the schedule knows about that
                # AssignedElectricalSystems missed - see
                # revit_utils.reconcile_circuits_with_schedule's
                # docstring for the full account. This runs for every
                # panel (both pre-existing and just-created schedules),
                # since a just-created schedule auto-places Revit's
                # already-assigned circuits into starting slots that
                # we've never read.
                slot_map = revit_utils.get_circuit_slot_map(psv, panel_info.num_slots)
                revit_utils.reconcile_circuits_with_schedule(
                    doc, panel_info, slot_map, panel_name,
                    phase_labels=balancing_service.phase_set_for_panel(panel_info),
                    panel_schedule_view=psv,
                )
                layout = panel_info.get_layout()
                layout_msg = _describe_layout(panel_name, layout)
                logger.info(layout_msg)
                _report_progress(progress_callback, i, total, panel_name, layout_msg)

                # STEP 2 (load balancing) now runs AFTER the capacity
                # adjustment and the spare/space reservation below - see
                # balancing_service.balance_panel's FIFTY-SECOND CORRECTION.

                # TWENTY-THIRD CORRECTION: determine spare/space needs
                # and RESERVE that trailing block of slots BEFORE
                # arrangement runs, rather than letting spare/space
                # fall into whatever gaps happen to remain afterward -
                # a user correctly pointed out this didn't reliably put
                # them at the bottom of the schedule. This also
                # finalizes the panel's right-sized capacity (grow or
                # shrink - see the ELEVENTH correction) up front, so
                # arrangement plans against the FINAL slot count from
                # the start instead of a capacity that might still
                # change afterward. Only applies in WaysTable mode;
                # Percentage mode keeps its simpler original behavior
                # of filling whatever's empty after arrangement.
                # TWENTY-SIXTH CORRECTION: is this panel's schedule
                # off-limits to physical rearrangement? True when the
                # project already had a schedule for it and the user
                # left "preserve existing panel schedules" on (the
                # default). Everything downstream that MOVES things -
                # capacity bump, R-Y-B arrangement, MoveSlotTo - is
                # gated on this. Placing spares/spaces into the free
                # slots that already exist is still allowed: that adds
                # to the schedule without disturbing anything issued.
                preserve = bool(
                    balancing_settings.preserve_existing_schedule
                    and panel_info.schedule_pre_existed
                )
                # FORTY-FIFTH CORRECTION: if Revit's own per-phase loads
                # contradict the phase layout read from the schedule, this
                # tool does not understand how this template maps slots to
                # phases. Moving circuits on that basis would scramble the
                # panel, so it is left alone and the reason is reported.
                if not preserve and not layout.trusted():
                    preserve = True
                    msg = (
                        "Panel '{}': NOT balanced. Only {:.0f}% of {} single-pole "
                        "circuits are on the phase this tool expects from the "
                        "schedule layout, so moving circuits could put them on "
                        "the wrong phase. No circuit was moved. Check the panel "
                        "schedule template's phase rows.".format(
                            panel_name, (layout.confidence or 0.0) * 100.0,
                            layout.checked)
                    )
                    logger.warning(msg)
                    _report_progress(progress_callback, i, total, panel_name,
                                     "WARNING: {}".format(msg))
                if preserve and layout.trusted():
                    _report_progress(
                        progress_callback, i, total, panel_name,
                        "Existing schedule preserved - no circuits will be "
                        "moved and capacity will not be resized; balancing "
                        "via spare load allocation."
                    )

                reserved_for_spare_space = set()
                spare_needed = space_needed = 0
                spare_rating = 20
                spare_space_poles = 1
                if spare_space_settings.mode == SPARE_SPACE_MODE_WAYS_TABLE:
                    # TWENTY-NINTH CORRECTION: count REAL connected
                    # circuits only. `len(panel_info.circuits)` also
                    # counted any Spare/Space rows the schedule scan
                    # picked up (they come back from GetCircuitByCell
                    # like any other circuit), which pushed the panel
                    # into a larger Ways bracket on every re-run - so
                    # running the tool twice kept growing the board.
                    connected = len(panel_info.real_circuits())
                    spare_needed, space_needed = spare_space_service.get_spare_space_counts_from_ways(
                        spare_space_settings.ways_table, connected,
                        spare_space_settings.spare_percentage,
                        spare_space_settings.space_percentage,
                    )
                    _matched_prefix, spare_rating, spare_space_poles = spare_space_service.match_panel_type(
                        panel_name, spare_space_settings.type_config_table
                    )
                    # FIFTY-FOURTH CORRECTION: the board must hold every POLE,
                    # not every circuit - a 3-pole breaker takes 3 ways. Sizing
                    # by circuit count asked Revit to shrink a 30-way board with
                    # multi-pole circuits to 24 ways; Revit refused ("number of
                    # poles exceeds Max Number of Single Pole Breakers") and the
                    # whole panel was skipped.
                    used_ways = sum(max(1, int(c.poles or 1)) for c in panel_info.real_circuits())
                    raw_target = used_ways + (spare_needed + space_needed) * spare_space_poles
                    candidates = spare_space_service.pick_standard_capacity(
                        spare_space_settings.ways_table, raw_target
                    )
                    target_capacity = candidates[0]

                    def _capacity_log(msg, _panel_name=panel_name, _i=i, _total=total):
                        logger.info(msg)
                        _report_progress(progress_callback, _i, _total, _panel_name, msg)

                    if preserve:
                        _capacity_log(
                            "Panel '{}': leaving capacity at {} ways - this "
                            "panel already had a schedule, so it is not being "
                            "resized. Spare/space will be fitted into the free "
                            "slots it already has.".format(
                                panel_name, panel_info.num_slots)
                        )
                    else:
                        before_slots = panel_info.num_slots
                        try:
                            with revit_utils.RevitTransaction(
                                doc, "Adjust Panel Capacity - {}".format(panel_name)
                            ):
                                panel_info.num_slots = revit_utils.bump_panel_capacity(
                                    doc, psv, panel_element, panel_name,
                                    target_capacity, panel_info.num_slots,
                                    log_fn=_capacity_log,
                                )
                        except Exception as resize_error:
                            # FIFTY-FOURTH CORRECTION: a refused resize is not a
                            # reason to skip the panel - keep the size it has and
                            # balance / fill spares within it.
                            panel_info.num_slots = revit_utils.get_total_slots(psv, doc=doc) or before_slots
                            _capacity_log(
                                "Panel '{}': Revit did not accept resizing to {} ways ({}). "
                                "Keeping {} ways and continuing.".format(
                                    panel_name, target_capacity,
                                    str(resize_error).split("Revit's reason:")[-1].strip()[:160],
                                    panel_info.num_slots))
                        # the capacity change committed: the schedule may
                        # have more or fewer rows now, so read it again
                        revit_utils.read_layout_into(panel_info, psv)
                        layout = panel_info.get_layout()

                        reserve_count = min(
                            (spare_needed + space_needed) * spare_space_poles,
                            panel_info.num_slots
                        )
                        # FIFTY-SECOND CORRECTION: only slots that are really
                        # free - existing circuits are never pushed out.
                        reserved_for_spare_space = arrangement_service.trailing_free_slots(
                            panel_info, reserve_count)

                # STEP 2: load-balance against the FINAL capacity and with
                # the spare/space slots already set aside, so every planned
                # move has a real free slot on its new phase.
                _report_progress(progress_callback, i, total, panel_name,
                                  "Balancing loads...")
                if layout.trusted():
                    bal_result = balancing_service.balance_panel(
                        panel_info, balancing_settings,
                        reserved_slots=reserved_for_spare_space)
                else:
                    # see the "NOT balanced" message above: report the
                    # loads, plan no change.
                    bal_result = balancing_service.balance_panel(
                        panel_info, _balancing_off(balancing_settings))
                bal_result.schedule_preserved = preserve
                for note in bal_result.notes:
                    logger.warning("Panel '%s': %s", panel_name, note)
                    _report_progress(progress_callback, i, total, panel_name,
                                     "WARNING: {}".format(note))

                slot_plan = {}
                if balancing_settings.arrange_ryb_after_balance and not preserve:
                    _report_progress(progress_callback, i, total, panel_name,
                                      "Arranging R-Y-B sequence...")
                    slot_plan, arrangement_warnings = arrangement_service.build_slot_plan(
                        panel_info, reserved_slots=reserved_for_spare_space
                    )
                    arrangement_service.validate_no_duplicate_slots(slot_plan)
                    for w in arrangement_warnings:
                        logger.warning("Panel '%s': %s", panel_name, w)
                        _report_progress(progress_callback, i, total, panel_name,
                                          "WARNING: {}".format(w))

                _report_progress(progress_callback, i, total, panel_name,
                                  "Writing panel schedule...")

                unresolved_moves = set()
                # FIFTIETH CORRECTION: the circuit moves get their OWN
                # transaction. They used to share one with the spare /
                # space fill, so a single spare Revit refused to place
                # rolled the balancing back with it.
                if not preserve:
                    with revit_utils.RevitTransaction(
                        doc, "Balance Circuits - {}".format(panel_name)
                    ):
                        planned_moves = len([
                            c for c in panel_info.circuits
                            if slot_plan.get(c.element_id) is not None
                            and c.slot is not None
                            and slot_plan[c.element_id] != c.slot
                        ])
                        unresolved_moves = _apply_slot_plan(
                            doc, panel_element, panel_info, slot_plan
                        ) or set()
                        # The reported "after" figures must describe the
                        # panel as it now IS, not as balancing hoped it
                        # would be - see _refresh_after_loads.
                        _refresh_after_loads(panel_info, bal_result)
                        move_msg = (
                            "Panel '{}': {} circuit(s) to move (phase balancing "
                            "and R-Y-B order of multi-pole breakers), "
                            "{} moved, {} could not be moved. Imbalance {:.1f}% "
                            "-> {:.1f}% (allowed {:.1f}%).".format(
                                panel_name, planned_moves,
                                planned_moves - len(unresolved_moves),
                                len(unresolved_moves),
                                bal_result.before_imbalance_pct,
                                bal_result.after_imbalance_pct,
                                balancing_settings.max_imbalance_pct)
                        )
                        logger.info(move_msg)
                        _report_progress(progress_callback, i, total,
                                         panel_name, move_msg)
                        if unresolved_moves:
                            msg = (
                                "{} circuit(s) could not be moved to their "
                                "balanced slots on panel '{}' and remain on "
                                "their original phase. The imbalance figures "
                                "reported below already account for this. "
                                "Increasing the panel's capacity before "
                                "re-running usually clears the "
                                "gridlock.".format(len(unresolved_moves),
                                                   panel_name)
                            )
                            logger.warning(msg)
                            _report_progress(progress_callback, i, total,
                                             panel_name,
                                             "WARNING: {}".format(msg))

                with revit_utils.RevitTransaction(
                    doc, "Spare and Space - {}".format(panel_name)
                ):
                    def log_fn(msg, _panel_name=panel_name, _i=i, _total=total):
                        logger.info(msg)
                        _report_progress(progress_callback, _i, _total, _panel_name, msg)

                    if spare_space_settings.mode == SPARE_SPACE_MODE_WAYS_TABLE:
                        _report_progress(progress_callback, i, total, panel_name,
                                          "Filling spare/space (with breaker rating)...")
                        phase_labels = balancing_service.phase_set_for_panel(panel_info)
                        spare_placed, space_placed, spare_cells = \
                            revit_utils.fill_spare_and_space(
                                doc, panel_element, psv, panel_name,
                                spare_needed, space_needed, spare_rating,
                                spare_space_poles,
                                spare_space_settings.ways_table, log_fn=log_fn,
                                preserve_capacity=preserve,
                                phase_labels=phase_labels,
                            )
                        # TWENTY-SEVENTH CORRECTION: now that the spares
                        # physically exist and we know which leg each one
                        # sits on, book the balanced load onto them. This
                        # is what makes the panel's phase totals reflect
                        # the NEW spare/space layout instead of leaving
                        # every spare reading 0 VA.
                        _apply_spare_loads(
                            psv, panel_info, bal_result, spare_cells,
                            spare_space_settings, spare_rating,
                            panel_name, log_fn,
                        )
                        ss_report = SpareSpaceReport(
                            panel_name=panel_name, spares_added=spare_placed,
                            spaces_added=space_placed, reserved_slots=0,
                            remaining_free_slots=0,
                        )
                    else:
                        occupied_slots = [c.slot for c in panel_info.circuits if c.slot]
                        empty_slots = revit_utils.get_empty_slots(
                            psv, panel_info.num_slots, occupied_slots=occupied_slots
                        )
                        spare_slots, space_slots, _reserved, ss_report = \
                            spare_space_service.plan_spare_space(
                                panel_info, spare_space_settings, empty_slots
                            )
                        for slot in spare_slots:
                            revit_utils.add_spare_or_space(psv, slot, is_spare=True)
                        for slot in space_slots:
                            revit_utils.add_spare_or_space(psv, slot, is_spare=False)

                # TWENTY-FIRST CORRECTION: definitive post-move
                # verification. A user reported 3-pole circuits still
                # not reading R-Y-B after the EIGHTEENTH correction's
                # step-of-2 planning fix. Rather than guess further,
                # re-scan the schedule's ACTUAL final positions (a
                # read-only call, safe outside a transaction) and check
                # whether each multi-pole circuit's real slots form a
                # genuine R-Y-B (3-pole) sequence. This distinguishes,
                # with certainty, a PLANNING issue (already caught by
                # build_slot_plan's own warnings, surfaced above) from
                # an APPLICATION issue - i.e. Revit's MoveSlotTo not
                # actually relocating a circuit's poles the way this
                # tool's step-of-2 convention assumes (the "still needs
                # live verification" caveat noted in the EIGHTEENTH
                # CORRECTION).
                # Nothing was moved on a preserved panel, so there is no
                # post-move state to verify - the schedule reads exactly
                # as it did before this run.
                actual_slot_map = (
                    {} if preserve
                    else revit_utils.get_circuit_slot_map(psv, panel_info.num_slots)
                )

                # FORTY-FIFTH CORRECTION: the "after" figures are now
                # taken from Revit itself. The update transaction has
                # committed, so each circuit's own per-phase apparent
                # load reflects where it really is. Everything reported
                # before this was this tool's belief about the result.
                if not preserve:
                    actual_after = revit_utils.read_actual_phase_loads(
                        doc, panel_info, layout.labels)
                    if actual_after:
                        believed = bal_result.after_imbalance_pct
                        _set_after_from_actual(panel_info, bal_result, actual_after)
                        check_msg = (
                            "Panel '{}': phase loads read back from Revit after "
                            "the update: {} - imbalance {:.1f}% (this tool "
                            "expected {:.1f}%).".format(
                                panel_name,
                                ", ".join("{} {:.0f} VA".format(p, actual_after[p])
                                          for p in layout.labels),
                                bal_result.after_imbalance_pct, believed)
                        )
                        logger.info(check_msg)
                        _report_progress(progress_callback, i, total,
                                         panel_name, check_msg)

                for circuit in panel_info.circuits:
                    if circuit.poles != 3:
                        continue
                    actual_start = actual_slot_map.get(circuit.element_id)
                    if actual_start is None:
                        continue
                    actual_slots = arrangement_service.circuit_slot_range(
                        actual_start, circuit.poles, layout
                    )
                    actual_phases = tuple(
                        str(arrangement_service.phase_for_slot(s, layout))
                        for s in actual_slots
                    )
                    if actual_phases != tuple(layout.labels)[:3] and circuit.element_id in unresolved_moves \
                            and actual_start == circuit.slot:
                        # FIFTY-THIRD CORRECTION: it never moved - it was on this
                        # run before the tool started, and could not be re-aligned.
                        msg = (
                            "Circuit '{}' (3-pole) is on slots {} which read as {} - "
                            "NOT R-Y-B. It was already there before this run; the "
                            "tool could not move it to an R-Y-B run (no free run of "
                            "3 ways in one column). Free 3 ways in a column and run "
                            "again, or move it in the schedule.".format(
                                circuit.description or circuit.circuit_number,
                                actual_slots, "-".join(actual_phases)))
                        logger.warning(msg)
                        _report_progress(progress_callback, i, total, panel_name,
                                         "WARNING: {}".format(msg))
                        bal_result.misaligned_3pole_circuits.append(
                            (circuit.description or circuit.circuit_number,
                             "-".join(actual_phases)))
                    elif actual_phases != tuple(layout.labels)[:3]:
                        msg = (
                            "VERIFICATION FAILED: circuit '{}' (3-pole) ended up "
                            "on slots {} after the move, which read as {} - NOT "
                            "R-Y-B order. This confirms the misalignment happened "
                            "during/after the actual Revit move (MoveSlotTo), not "
                            "just in planning - the tool's step-of-2 slot "
                            "convention may not match how Revit itself relocates "
                            "this circuit's poles.".format(
                                circuit.description or circuit.circuit_number,
                                actual_slots, "-".join(actual_phases)
                            )
                        )
                        logger.warning(msg)
                        _report_progress(progress_callback, i, total, panel_name,
                                          "WARNING: {}".format(msg))
                        bal_result.misaligned_3pole_circuits.append(
                            (circuit.description or circuit.circuit_number,
                             "-".join(actual_phases))
                        )

                balancing_results.append(bal_result)
                spare_space_reports.append(ss_report)

            except Exception as ex:
                logger.exception(
                    "Panel '%s' failed and was skipped. Per the TENTH "
                    "CORRECTION (see revit_utils.py module docstring), "
                    "schedule creation now commits in its own transaction "
                    "before this point, so if this panel's schedule was "
                    "just created, it has been KEPT even though the panel "
                    "is reported as failed - only work AFTER schedule "
                    "creation (balancing/arrangement/spare-space) was "
                    "rolled back. Earlier panels in this batch are "
                    "unaffected either way.", panel_name
                )
                failed_panels.append((panel_name, str(ex)))
                _report_progress(progress_callback, i, total, panel_name,
                                  "FAILED: {} - skipping to next panel.".format(ex))
                continue

            _report_progress(progress_callback, i + 1, total, panel_name,
                              "Done.")

    return balancing_results, spare_space_reports, failed_panels


def _balancing_off(settings):
    """A copy of the balancing settings with balancing switched off."""
    from panelmgr.models import BalancingSettings
    return BalancingSettings(
        enabled=False,
        max_imbalance_pct=settings.max_imbalance_pct,
        respect_locked_circuits=settings.respect_locked_circuits,
        arrange_ryb_after_balance=settings.arrange_ryb_after_balance,
        preserve_existing_schedule=settings.preserve_existing_schedule,
        balance_via_spare_loads=settings.balance_via_spare_loads,
    )


def _describe_layout(panel_name, layout):
    """One log line saying how this schedule maps slots to phases."""
    if layout.slot_phase is None:
        return ("Panel '{}': schedule layout could not be read - assuming a "
                "two-column R-Y-B layout.".format(panel_name))
    first = [str(layout.phase(s)) for s in sorted(layout.slot_phase)[:6]]
    kind = ("one column (a 3-pole breaker takes slots 1-2-3)"
            if layout.slot_below.get(1) == 2
            else "two columns (a 3-pole breaker takes slots 1-3-5)")
    if layout.confidence is None:
        check = "no loaded single-pole circuit to check it against"
    else:
        check = "{:.0f}% of {} single-pole circuits agree with Revit's own phase loads".format(
            layout.confidence * 100.0, layout.checked)
    return ("Panel '{}': schedule layout read from Revit - {}, slots 1-6 are on "
            "{}; {}.".format(panel_name, kind, "-".join(first), check))


def _set_after_from_actual(panel_info, bal_result, actual):
    """Replace the 'after' VA figures with the ones read back from Revit."""
    from panelmgr.models import PhaseLoadResult
    counts = dict((p, pl.circuit_count)
                  for p, pl in (bal_result.after_phase_loads or {}).items())
    bal_result.after_phase_loads = dict(
        (p, PhaseLoadResult(p, va, counts.get(p, 0))) for p, va in actual.items()
    )
    bal_result.after_imbalance_pct = balancing_service.compute_imbalance_percentage(
        bal_result.after_phase_loads
    )


def _resolve_spare_load_per_pole(panel_info, spare_space_settings, spare_rating):
    """
    The assumed VA each spare POLE represents. Either the explicit
    `spare_load_va_per_pole` setting, or - when that is 0 (the
    default) - derived from the spare breaker's rating and the panel's
    line-to-neutral voltage, which is the figure most spec sections
    mean by "show spare capacity on the schedule". A diversity factor
    is applied either way.
    """
    voltage = panel_info.voltage or 208.0
    # Line-to-neutral is what a single pole actually sees on a 3-phase
    # wye board; 208 V and 400 V are line-to-line figures.
    if panel_info.phase_config != "SinglePhase" and voltage > 150:
        line_to_neutral = voltage / (3 ** 0.5)
    else:
        line_to_neutral = voltage
    base = spare_space_settings.spare_load_va_per_pole
    if not base or base <= 0:
        base = float(spare_rating) * line_to_neutral
    return base * (spare_space_settings.spare_diversity_factor or 1.0)


def _apply_spare_loads(psv, panel_info, bal_result, spare_cells,
                       spare_space_settings, spare_rating, panel_name, log_fn):
    """
    TWENTY-SEVENTH CORRECTION. Books the balanced spare load onto the
    spares that were just placed.

    `spare_cells` comes back from `revit_utils.fill_spare_and_space` as
    [{"row", "col", "poles", "phases"}]. Each (cell, leg) pair is
    treated as one unit of spare capacity, so a 3-pole spare
    contributes to all three legs. `balancing_service.plan_spare_balance`
    decides how much of the total declared spare capacity each leg
    should carry - the lightest leg gets the most - and the result is
    written with `PanelScheduleView.SetSpareLoadValue`.

    Must be called from inside an open Transaction.
    """
    if not spare_cells:
        return
    if not spare_space_settings or not bal_result:
        return

    per_pole_va = _resolve_spare_load_per_pole(
        panel_info, spare_space_settings, spare_rating
    )

    # {(row, col, phase): phase} - one entry per spare pole.
    spare_slot_phases = {}
    for idx, cell in enumerate(spare_cells):
        for phase in cell.get("phases") or []:
            if phase is None:
                continue
            spare_slot_phases[(idx, phase)] = phase

    if not spare_slot_phases:
        return

    per_slot_va, allocation = balancing_service.plan_spare_balance(
        panel_info, bal_result, spare_slot_phases, per_pole_va
    )

    written = {}
    for idx, cell in enumerate(spare_cells):
        phase_va_map = {}
        for phase in cell.get("phases") or []:
            if phase is None:
                continue
            phase_va_map[phase] = per_slot_va.get((idx, phase), 0.0)
        if not phase_va_map:
            continue
        if revit_utils.set_spare_slot_loads(
            psv, cell["row"], cell["col"], phase_va_map,
            panel_name=panel_name, log_fn=log_fn
        ):
            for phase, va in phase_va_map.items():
                written[phase] = written.get(phase, 0.0) + va

    bal_result.spare_load_written_va = written

    projected = balancing_service.projected_phase_loads(panel_info, written)
    projected_pct = balancing_service.compute_imbalance_percentage(projected)
    log_fn(
        "Panel '{}': booked {:.0f} VA of spare capacity as {} - projected "
        "phase imbalance including spares is {:.1f}% (connected-only was "
        "{:.1f}%).".format(
            panel_name, sum(written.values()),
            ", ".join("{} {:.0f} VA".format(p, v)
                      for p, v in sorted(written.items())),
            projected_pct, bal_result.after_imbalance_pct,
        )
    )

def _apply_slot_plan(doc, panel_element, panel_info, slot_plan):
    """
    Applies the planned slot numbers to the live Revit circuits.

    TWELFTH CORRECTION (significant): this used to try
    `revit_utils.try_set_circuit_number` FIRST - writing
    `BuiltInParameter.RBS_ELEC_CIRCUIT_NUMBER` - falling back to
    `swap_schedule_slots` (true physical repositioning via MoveSlotTo)
    only if that failed. A live run showed a panel's connected loads
    remaining just as imbalanced after "balancing" as before, even
    though the tool reported success. The likely explanation: a
    circuit's real phase connection in a panelboard is determined by
    its PHYSICAL SLOT position, not by the displayed "Circuit Number"
    label - `try_set_circuit_number`'s docstring itself already noted
    this numbering is normally DERIVED from slot position rather than
    freely editable, meaning the parameter write could succeed (no
    exception, not read-only) while doing nothing to actually move the
    circuit to a different phase - a false positive. This function now
    uses `swap_schedule_slots` (MoveSlotTo) as the ONLY mechanism for
    reassigning a circuit's phase; `try_set_circuit_number` is no
    longer called from this path. It also now requires a genuine,
    previously-discovered current slot (`circuit.slot`, populated by
    `revit_utils.get_circuit_slot_map` before this is called) to know
    where to move FROM - a circuit whose current slot isn't known is
    skipped with a warning rather than silently doing nothing.

    THIRTEENTH CORRECTION: a live comparison of before/after schedules
    on a nearly-fully-packed panel showed per-phase totals completely
    UNCHANGED, even with the TWELFTH CORRECTION in place - because
    `MoveSlotTo` has no atomic swap (a destination must already be
    free), a single pass of direct moves gridlocks completely when
    almost every candidate slot is already occupied by ANOTHER circuit
    that ALSO needs to move (exactly the case on a panel with little
    to no free capacity to begin with). This function now runs
    MULTIPLE passes: each pass executes every move whose destination
    happens to be free at that moment (which grows the free-slot pool
    as moves complete), and if a full pass makes no progress at all
    (true gridlock/cycle), it stages ONE pending circuit into any
    genuinely free slot in the panel to break the deadlock, re-queuing
    it to reach its real target on a later pass. If there is truly no
    free slot anywhere in the panel to stage through, this is logged
    clearly (increasing the panel's capacity - see
    `revit_utils.bump_panel_capacity` - before arrangement would
    resolve it) rather than silently giving up.

    FORTY-THIRD CORRECTION: this function returned nothing, and left
    every circuit's `phases` set to whatever balancing WANTED. For a
    circuit whose move could not be resolved (gridlock, or MoveSlotTo
    refusing) the circuit stayed physically where it was while its
    in-memory phase still claimed the new leg, so the results grid,
    the report and the summary all reported a balance the panel does
    not have - the tool's own log said "could not be moved" one line
    after computing an "after" figure that assumed it had. Phases are
    now re-derived from each circuit's REAL final slot, and the set of
    unresolved element ids is returned so the caller can refresh the
    reported figures and surface the shortfall.
    """
    from panelmgr.revit_utils import element_id as _element_id

    layout = panel_info.get_layout()
    pending_moves = {}
    circuits_by_id = {}
    for circuit in panel_info.circuits:
        target_slot = slot_plan.get(circuit.element_id)
        if target_slot is None or circuit.slot is None or circuit.slot == target_slot:
            continue
        current_slots = arrangement_service.circuit_slot_range(circuit.slot, circuit.poles, layout)
        target_slots = arrangement_service.circuit_slot_range(target_slot, circuit.poles, layout)
        pending_moves[circuit.element_id] = (current_slots, target_slots)
        circuits_by_id[circuit.element_id] = circuit

    if not pending_moves or not panel_info.panel_schedule_view_id:
        return set()

    psv = doc.GetElement(_element_id(panel_info.panel_schedule_view_id))
    # NINETEENTH CORRECTION: use circuit_slot_range (step-of-2,
    # same-column) as the single source of truth for which slots a
    # multi-pole circuit occupies, rather than the step-of-1 range
    # this used before - see arrangement_service's EIGHTEENTH
    # CORRECTION for why step-of-1 was wrong given phase_for_slot's
    # own 2-slots-per-phase convention, and its resolver docstring for
    # why an incomplete/inconsistent occupied set caused the resolver
    # to keep retrying doomed moves against slots it incorrectly
    # believed were free.
    occupied = set()
    for c in panel_info.circuits:
        if c.slot:
            occupied.update(arrangement_service.circuit_slot_range(c.slot, c.poles, layout))

    def move_fn(from_slot, to_slot):
        return revit_utils.swap_schedule_slots(psv, from_slot, to_slot)

    stranded = {}
    resolved, unresolved = arrangement_service.resolve_moves_with_staging(
        pending_moves, occupied, panel_info.num_slots, move_fn,
        run_fn=lambda start, count: layout.run(start, count, strict=True),
        final_positions=stranded,
    )

    for eid, final_slot in resolved.items():
        circuits_by_id[eid].slot = final_slot
    # staged but neither at its target nor back home: it really is here
    for eid, final_slot in stranded.items():
        circuits_by_id[eid].slot = final_slot
        logger.warning("Circuit '%s' was parked on slot %s during the moves and "
                       "could not be returned to its original slot.",
                       circuits_by_id[eid].description or circuits_by_id[eid].circuit_number,
                       final_slot)

    for eid in unresolved:
        circuit = circuits_by_id[eid]
        _current_slots, target_slots = pending_moves[eid]
        logger.warning(
            "Circuit '%s' could not be moved to its balanced slot %s - "
            "it remains on its original phase (arrangement gridlock, or "
            "no free slot anywhere in the panel to stage a "
            "deadlock-breaking move through).",
            circuit.description or circuit.circuit_number, target_slots[0]
        )

    # Re-derive every circuit's phase from the slot it ACTUALLY ends up
    # on (see this function's FORTY-THIRD CORRECTION). Moves that
    # landed and moves that did not are treated identically here -
    # the slot is the truth either way.
    for circuit in panel_info.circuits:
        if circuit.slot is None:
            continue
        circuit.phases = tuple(
            leg for leg in (
                layout.phase(s) for s in arrangement_service.circuit_slot_range(
                    circuit.slot, circuit.poles, layout)
            ) if leg is not None
        )

    return unresolved


def _leg_for_slot(slot_number, phase_labels):
    """
    Which leg a 1-based slot sits on, cycling over the panel's OWN leg
    names. `arrangement_service.phase_for_slot` hardcodes the
    three-leg R-Y-B cycle, which is wrong for split-phase (L1/L2)
    boards - see revit_utils.reconcile_circuits_with_schedule's
    THIRTY-NINTH CORRECTION for the damage that caused.
    """
    if not phase_labels:
        return None
    return phase_labels[(((slot_number - 1) // 2) % len(phase_labels))]



def _report_progress(callback, index, total, panel_name, message):
    if callback:
        try:
            callback(index, total, panel_name, message)
        except Exception:
            logger.exception("progress_callback raised - ignoring")

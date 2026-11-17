# Corrections 25–31

Debugging pass covering the phase-load math, split-phase panels,
Spare/Space handling, and two new behaviours: **preserving existing
panel schedules** and **booking balanced loads onto spares**.

All 47 unit tests pass (30 pre-existing + 17 new):

```
PYTHONPATH=lib python -m pytest tests/ -q
```

---

## 25. Multi-pole load was counted in full on every leg

**Severity: critical, silent, present since v1.**

`_fixed_multipole_loads` and `compute_phase_loads` both did:

```python
for ph in c.phases:
    totals[ph] += c.apparent_load_va      # WRONG
```

`apparent_load_va` is the circuit's **total** apparent load across all
its poles, not a per-leg figure. A 3-pole 9 kVA circuit was therefore
booked as 9 kVA on R *and* 9 kVA on Y *and* 9 kVA on B — 27 kVA of
phantom load.

Unlike the 22nd correction's unit-scaling issue, this does **not**
cancel out of the relative comparison, because the inflation factor
differs per circuit (×3 for 3-pole, ×2 for 2-pole, ×1 for 1-pole). It
actively skewed which leg the LPT heuristic believed was lightest, so
it produced genuinely wrong balancing decisions on any panel with a
mix of pole counts.

**Fix:** `CircuitInfo.phase_load_va(phase)` / `phase_load_w(phase)`
return `total / len(self.phases)`, matching how Revit itself populates
`RBS_ELEC_APPARENT_LOAD_PHASEA/B/C`. Every summation now goes through
those.

Tests: `test_three_pole_load_splits_evenly_across_legs`,
`test_two_pole_load_splits_across_its_two_legs`,
`test_mixed_pole_panel_balances_on_true_per_leg_load`.

---

## 26. Existing panel schedules are no longer rearranged

**New behaviour — this is the default.**

`build_panel_info` now records `PanelInfo.schedule_pre_existed` (True
when the project already had a `PanelScheduleView` for this panel, as
opposed to one this tool is about to create).

When `BalancingSettings.preserve_existing_schedule` is on (default)
and the schedule pre-existed, `execute_batch` skips:

| Step | Previously | Now |
|---|---|---|
| `bump_panel_capacity` | grew **and shrank** the board | skipped entirely |
| trailing slot reservation | reserved a block at the bottom | skipped |
| `arrangement_service.build_slot_plan` | re-planned every slot | skipped |
| `_apply_slot_plan` → `MoveSlotTo` | physically relocated circuits | skipped |
| post-move R-Y-B verification scan | re-scanned the schedule | skipped (nothing moved) |

Spares and spaces are **still** placed into the free slots the panel
already has — that adds to the schedule without disturbing anything
issued. `fill_spare_and_space(preserve_capacity=True)` confines
placement to the existing capacity and reports a shortfall rather than
silently resizing the board.

Rationale: an issued schedule is a coordinated drawing. Relocating
real circuits invalidates every drawing, cable schedule and label that
references those circuit numbers. The moves were also never reliable —
see the existing 12th/13th corrections (MoveSlotTo gridlock on packed
panels) and the 21st (3-pole circuits not landing R-Y-B after the
move).

Panels whose schedule this tool creates fresh are still arranged
normally; there is nothing issued to protect.

UI: **Step 2 → "Preserve existing panel schedules (do not move
circuits or resize)"**.

Tests: `test_preserve_mode_moves_nothing`,
`test_new_schedule_still_gets_rearranged`,
`test_preserve_can_be_turned_off`.

---

## 27. Spare/Space loads are now adjusted to the new spare and space

**New behaviour.** `PanelScheduleView.SetSpareLoadValue` was never
called anywhere in the codebase. Spares were placed with a pole count
and a breaker rating but **no load**, so every spare read 0 VA and the
panel's loads summary did not reflect the new spare/space layout at
all — the schedule declared spare ways without declaring the capacity
they reserve, which is the figure a reviewer checks.

Three pieces:

1. **`balancing_service.allocate_spare_load(fixed, budget, phases)`** —
   classic water-filling. Repeatedly tops up the lightest leg(s) to the
   level of the next-lightest until the budget runs out; remainder
   split evenly. Never returns a negative allocation (you cannot
   un-book connected load), so a leg already heavier than the target
   simply gets nothing.

2. **`revit_utils.fill_spare_and_space`** now returns
   `(spare_placed, space_placed, spare_cells)`, where each spare cell
   carries `{"row", "col", "poles", "phases"}` — the legs its rows sit
   on, derived via `phase_for_row` using the same
   row-mod-3 model `_find_free_run` already uses for phase-aligned
   placement, so the two can't drift apart.

3. **`revit_utils.set_spare_slot_loads`** writes each leg with
   `SetSpareLoadValue(row, col, ElementId(RBS_ELEC_APPARENT_LOAD_PHASEA/B/C), v)`
   plus `RBS_ELEC_APPARENT_LOAD` as the sum, so the spare's own row and
   the per-leg summary agree.

Spaces are excluded by design — no breaker, no reserved capacity, and
Revit raises `ArgumentsInconsistentException` for a non-spare cell.

Per-pole VA defaults to `breaker rating × line-to-neutral voltage`
(line-to-line ÷ √3 on a 3-phase wye board), times
`SpareSpaceSettings.spare_diversity_factor`. Override with
`spare_load_va_per_pole`.

Balancing decides only **where** the declared spare capacity lands,
never how much of it there is — the spare/space counts stay exactly as
the Ways table or percentage settings determined.

Worked example (7000 / 4000 / 3000 VA connected, 6 spare poles at
2400 VA):

```
connected legs      R 7000   Y 4000   B 3000    imbalance 85.7%
spare allocation    R 2467   Y 5467   B 6467    (14400 VA budget)
projected legs      R 9467   Y 9467   B 9467    imbalance  0.0%
circuits moved      0
```

Tests: `test_allocate_spare_load_levels_the_legs`,
`test_allocate_spare_load_never_goes_negative`,
`test_plan_spare_balance_puts_most_load_on_lightest_leg`,
`test_projected_loads_include_spare_allocation`,
`test_spare_balance_does_not_change_the_spare_budget`.

---

## 28. Single-phase panels crashed outright

**Severity: hard failure.** `balance_panel` assigned split-phase
circuits to `("L1",)` / `("L2",)` via `_phase_set_for_panel`, then
handed them to `compute_phase_loads`, whose totals dict was hardcoded
to `PHASES = ("R","Y","B")`:

```
KeyError: 'L1'
```

Caught by `execute_batch`'s per-panel try/except, so every
single-phase panel in a batch was silently reported **FAILED** and
skipped. Separately, `_fixed_multipole_loads` re-keyed its result with
`.get(p, 0.0)` against L1/L2, dropping 100% of multi-pole load on
those panels.

**Fix:** `compute_phase_loads(..., phase_set=None)` takes the panel's
actual leg set and accumulates any unexpected leg rather than raising.
`phase_set_for_panel` is now public and used consistently by both the
balancing and reporting paths.

Tests: `test_single_phase_panel_does_not_crash`,
`test_single_phase_multipole_load_is_not_dropped`.

---

## 29. Spares and Spaces were treated as real circuits

`_build_circuit_info` never set `is_spare` / `is_space` — both stayed
False for every circuit it produced. That matters because Spare and
Space rows **are** returned by `GetCircuitByCell` as circuit-like
objects (which is exactly why `count_connected_ways` has to filter
them out by `LoadName`), and `reconcile_circuits_with_schedule`
appends whatever the schedule scan finds into `panel_info.circuits`.

Net effect: existing spares and spaces
- inflated the connected-ways count driving the Ways bracket lookup,
  so **re-running the tool kept growing the board**;
- were fed into the balancing pool as movable load;
- could have `MoveSlotTo` planned for them by arrangement.

**Fix:** detected by `LoadName` in `_build_circuit_info`, matching the
discriminator `count_connected_ways` already used.
`PanelInfo.real_circuits()` and `CircuitInfo.is_placeholder()` added;
`compute_phase_loads`, the balancing pool, and both the preview and
execute connected-count paths now use them.

Tests: `test_spare_and_space_excluded_from_phase_totals`,
`test_real_circuits_filters_placeholders`.

---

## 30. Panel name was read from the wrong parameter

`build_panel_info` used `panel_element.Name`, which on a
`FamilyInstance` returns the family **type** name ("225A 42-circuit
panelboard"), not the panel's own "Panel Name" ("LP-2", "ACP-01").

Every downstream consumer assumes the latter — in particular
`match_panel_type` matches its prefixes (PP / LP / ELP / UDB / ACP)
against this string. Matching against a type name meant the lookup
essentially never hit, so **every panel silently fell back to the
DEFAULT 20 A / 1-pole spare** regardless of what the Type Config table
said. It also mislabelled every log line and report row.

**Fix:** `BuiltInParameter.RBS_ELEC_PANEL_NAME`, falling back to the
old behaviour only if the parameter is genuinely absent.

---

## 31. Preview and Execute could disagree

`_clone_panel_info` did not copy `schedule_pre_existed`, so Preview
would show REARRANGE numbers for a panel that Execute would actually
PRESERVE. Both paths also used `len(panel_info.circuits)` for the
connected-ways count, inheriting bug 29.

**Fix:** the flag is carried across the clone, and both paths use
`real_circuits()`.

---

## Reporting

On a preserved panel, "Imbalance After" is necessarily identical to
"Imbalance Before" — nothing moved, and nothing could without
invalidating the issued schedule. Reporting only that figure would
make a preserved panel look like the tool did nothing, so
`build_report_rows` gained:

- **Schedule** — `Preserved` or `Rearranged`
- **Spare Load Booked (VA)**
- **Spare Load by Phase (VA)**
- **Imbalance Incl. Spares (%)** — the projected figure once the new
  spare capacity is counted, which is the number that actually
  improved

---

## Known issue left open (not a regression)

`_va_from_internal_units` / `_va_to_internal_units` use the
empirically-derived `0.3048**2` constant (22nd correction). The
supported equivalent is
`UnitUtils.ConvertToInternalUnits(va, UnitTypeId.VoltAmperes)` and its
inverse, which is version-safe. `UnitTypeId` requires Revit 2021+;
`DisplayUnitType` before that. Left as-is deliberately so this pass
does not also shift every previously-reported figure — worth
switching once the minimum supported version is confirmed.

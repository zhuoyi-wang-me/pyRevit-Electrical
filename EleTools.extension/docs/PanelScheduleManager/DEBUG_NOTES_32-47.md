# Debugging pass: corrections 32-47

Follow-on to `CORRECTIONS_25-31.md`. Every item below is a bug that was
present in the uploaded build. The test suite passed 47/47 against that
build, so none of these were caught by it - they all sat in code the
suite does not reach (`revit_utils`, `schedule_service`'s Revit path,
the viewmodels, `config`, the XAML) or in code paths the suite did not
exercise (split-phase panels, locked multi-pole circuits).

Suite is now **63 tests, all passing**, and runs with a bare
`python -m pytest tests/`.

---

## Verified against Autodesk documentation

### 37. `RevitTransaction` never suppressed a single dialog
`revit_utils.RevitTransaction`

Failure-handling options were configured in `__init__`, i.e. before
`Transaction.Start()`. The Revit API documents that *"every time a
transaction starts, Failure Handling Options will be reset to their
default values."* `Start()` therefore discarded `SilentFailuresPreprocessor`
every single time, and every batch ran with Revit's stock modal
dialogs - exactly what the class exists to prevent. There was no visible
symptom beyond dialogs interrupting a run meant to be unattended,
because the original code swallowed any failure into a warning.

Options are now applied from `__enter__`, after `Start()`.

### 38. `ElementId.IntegerValue` / `ElementId(int)` throughout
`revit_utils`, `schedule_service`

Both were deprecated in the Revit 2024 API in favour of `ElementId.Value`
/ `ElementId(Int64)`. They are obsolete-but-present in 2025 and **absent
in 2026 and 2027**. Two concrete consequences, not just a compiler
warning:

1. `IntegerValue` raises `InvalidOperationException` when an id needs
   more than 32 bits. Ids that large are normal in big and cloud-hosted
   models, so on those projects the tool threw while merely *reading* a
   panel - before doing any work at all.
2. On Revit 2026 the member does not exist, so the extension cannot
   load, despite `extension.json` declaring only a *minimum* of 2025.

Added `eid_value()` / `element_id()` helpers that prefer the modern API
and fall back to the legacy one; migrated all 12 call sites. Note the
`ElementId(BuiltInParameter)` constructor used by `SetSpareLoadValue` is
a different, non-deprecated overload and was left alone.

---

## Logic bugs

### 39. Split-phase panels produced fictional imbalance figures
`revit_utils.reconcile_circuits_with_schedule`

Phases were derived from the hardcoded three-leg R-Y-B cycle for
*every* panel, but `balancing_service.phase_set_for_panel` models
120/240 V boards as two legs, L1/L2. On a single-phase panel:

- `_fixed_multipole_loads` keys only L1/L2, so multi-pole load sitting
  on an "R" leg was dropped from the running totals entirely;
- `compute_phase_loads` - which since the 28th correction accumulates
  unknown legs rather than raising `KeyError` - returned up to **five**
  legs (L1, L2, R, Y, B), two of which were always 0.0, so
  `compute_imbalance_percentage` divided by a five-way average.

A perfectly balanced split-phase board reported a large imbalance. The
28th correction stopped the crash but left the wrong answer behind.
Leg names now follow the panel's own phase configuration.

### 40. Locked multi-pole circuits caused physical slot collisions
`arrangement_service.build_slot_plan`

Only the circuit's *primary* slot was reserved. A locked 3-pole circuit
at slot 7 occupies 7, 9 and 11, but 9 and 11 stayed in the free pool and
were handed to other circuits - two breakers planned onto one physical
position. `validate_no_duplicate_slots` cannot catch this, because the
plan only ever stores a multi-pole circuit's primary slot, so the two
entries never look like duplicates. Now reserves the full
`circuit_slot_range`.

### 41. Failed moves were reported as successes
`schedule_service._apply_slot_plan`

The function returned nothing and left every circuit's `phases` set to
whatever balancing *wanted*. When a move could not be resolved
(gridlock, or `MoveSlotTo` refusing), the circuit stayed physically put
while its in-memory phase claimed the new leg - so the results grid,
the report and the summary all described a balance the panel does not
have. The tool's own log said "could not be moved" one line after
computing an "after" figure that assumed it had. This is the same
false-success the 12th and 13th corrections were chasing.

Phases are now re-derived from each circuit's real final slot, the
unresolved set is returned, and `execute_batch` refreshes the reported
figures and surfaces the shortfall.

### 42. Preview promised work Execute was designed not to do
`schedule_service.run_preview`

Two divergences from `execute_batch`:

- Preserve mode was ignored. `balance_panel` correctly returned a
  PRESERVE result, then `run_preview` built a full R-Y-B slot plan for
  the panel anyway - a rearrangement Execute gates itself against.
- Spare/space was planned *after* arrangement; Execute reserves that
  trailing block *before* (23rd correction). The two computed slot
  plans against different free-slot pools.

Also, Percentage-mode free slots were derived from `len(real_circuits())`,
counting a 3-pole breaker as one slot.

### 43. Four settings the UI exposes were never persisted
`config.py`

`preserve_existing_schedule`, `balance_via_spare_loads`,
`spare_load_va_per_pole` and `spare_diversity_factor` were absent from
the defaults, the loader and the writer. The Step 2/3 UI binds them, the
user changes them, and they were discarded on every save and reset to
model defaults on every restart.

`load_settings` also returned a shallow copy, so anything that mutated
the result corrupted the module-level defaults for the rest of the
session. (`_deep_merge(x, {})` does *not* fix this - it only copies
branches the override mentions. Uses `copy.deepcopy` now.)

### 44. The diagnostic log file was always empty inside Revit
`logger.py`

The rotating file handler was attached to a logger named `"panelmgr"`,
but `get_logger` returns *pyRevit's* logger, which lives under a
different name and is therefore not in that hierarchy. Nothing the tool
logged ever reached the file the module docstring promises for
diagnosing field issues. The handler is now attached to whichever logger
is actually handed back.

### 45. Results grid silently dropped three columns
`MainWindow.xaml`, `panel_item_viewmodel`, `report_service`

The header-to-property mangling stripped spaces, parentheses and `%` but
not the **dot** in `"Imbalance Incl. Spares (%)"`, producing
`ImbalanceIncl.SparesPct`. Python's `setattr` accepts that; WPF parses it
as a nested property *path* (`ImbalanceIncl` -> `SparesPct`), which can
never resolve - and WPF renders an unresolvable binding as a blank cell,
not an error.

Separately, `Schedule`, `Spare Load Booked (VA)` and
`Imbalance Incl. Spares (%)` existed in the report rows and the HTML
export but had no column in the grid at all, so a *preserved* panel
looked in the UI like the tool had done nothing to it.

`result_property_name` now strips everything non-alphanumeric, and moved
to `report_service` (next to the headers it must stay in step with, and
importable outside Revit - the viewmodel package imports `clr`). Columns
added; FAILED rows carry the same key set so their cells bind.

### 46. Pushbutton was enabled with no document open
`bundle.yaml`

`context: zero-doc` tells pyRevit the button is valid when *no* document
is open - the opposite of what this tool needs. `script.py`'s own
`doc is None` guard was the only thing between the user and a crash.
Now `doc-project`. Also removed `tooltip_image: tooltip.png`, which
points at a file not present in the bundle.

### 47. The test suite could not be run as documented
`tests/`

`test_services.py` did its own `sys.path.insert`; `test_balancing_fixes.py`
only documented `PYTHONPATH=lib` in its docstring. pytest imports test
modules alphabetically, so `test_balancing_fixes` came first and a plain
`python -m pytest tests/` aborted the whole run during collection with
`ModuleNotFoundError: No module named 'panelmgr'`, before the module that
fixes the path was reached. Added `tests/conftest.py`.

---

## Smaller fixes

- **32.** `PanelInfo.occupied_slots()` / `free_slot_count()` counted
  `CircuitInfo` *objects*, so a 3-pole breaker counted as one slot.
  Overstated free capacity by `(poles - 1)` per circuit. Added
  `used_slot_count()`.
- **35.** In `balance_panel`, a locked circuit with no discovered phase
  hit a gap: `ph` defaulted to `phase_set[0]`, which *is* in
  `running_totals`, so the `c.phases = (ph,)` repair never ran;
  `phase_load_va` then returned 0.0 for a phase-less circuit. Its whole
  load vanished from the totals and it stayed phase-less thereafter.
- **34.** `build_slot_plan`'s single-pole "borrow" fallback took the
  first non-empty bucket in R-Y-B order despite a comment claiming
  "least-loaded", piling every overflow circuit onto R. It also left the
  circuit's `phases` pointing at the intended leg rather than the one it
  landed on. Now picks the bucket with the most free slots, re-points
  the phase, and warns.
- **33.** Tidied `build_slot_plan`'s multi-pole assignment, which wrote
  `plan[element_id]` three times per circuit via a confusing
  `plan.get(...)` expression before overwriting it anyway.
- **48.** `export_html` did not escape. Panel names routinely contain
  `&` ("LP-1 & 2"), and FAILED rows embed arbitrary Revit exception
  text, which can contain `<`. Either corrupts the table, and exception
  text containing markup went straight into a file the user opens in a
  browser.
- **49.** `export_csv` opened the file in text mode with an explicit
  encoding, which is correct on CPython 3 and raises `TypeError` on
  every write under IronPython 2.7 (where `csv` emits bytes). Never
  noticed because only `export_html` is wired to the UI.
- Removed dead `electrical_data` local in `get_all_electrical_panels`
  and several unused imports. `pyflakes` is now clean across `lib/` and
  `PanelTools.tab/`.

---

## Not changed - worth a decision

- **`try_set_circuit_number` is now dead code.** Kept deliberately per
  the 12th correction's note, but nothing calls it.
- **`set_spare_slot_load` (singular) is unused** since
  `set_spare_slot_loads` replaced it, and the two disagree: the singular
  writes the same value to both the per-phase parameter and the total,
  where the plural correctly writes the sum as the total.
- **`extension.json` still declares `min_revit_version: 2025`.** With
  correction 38 the code should now load on 2026+, but that is untested
  against a real 2026 install.
- **The 0.3048² VA conversion constant** remains empirically derived.
  The 22nd correction's own note recommends replacing it with
  `UnitUtils.ConvertToInternalUnits(va, UnitTypeId.VoltAmperes)`, which
  is safe given the 2025 minimum. Left alone because it would change
  every reported figure in the same commit as these fixes.
- **Nothing here has been run against live Revit.** All fixes are either
  covered by the pure-Python suite or verified against the API
  documentation; corrections 37, 38 and 39's Revit-side effects need a
  real model to confirm.

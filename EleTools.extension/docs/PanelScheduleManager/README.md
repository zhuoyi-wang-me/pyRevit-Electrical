# Panel Schedule Manager — pyRevit Extension (Revit 2025)

> **Corrections 25–31 (latest debugging pass)** — see
> [`CORRECTIONS_25-31.md`](CORRECTIONS_25-31.md). Highlights: multi-pole
> load was being counted in full on every phase leg (3× inflation on
> 3-pole circuits, which corrupted balancing decisions); single-phase
> panels crashed with `KeyError: 'L1'` and were silently reported
> FAILED; Spares/Spaces were treated as real circuits, so re-running
> the tool kept growing the board. Two new behaviours:
> **existing panel schedules are preserved by default** (no
> `MoveSlotTo`, no capacity resize) and **spare slots now get balanced
> loads written via `SetSpareLoadValue`**, so the panel's phase totals
> reflect the new spare/space layout.


Automated electrical panel schedule generation, load balancing, spare/space
management, and R‑Y‑B circuit arrangement, with a 5‑step wizard UI in the
style of DiRoots tools.

---

## 1. System Architecture

```
                        ┌─────────────────────────┐
                        │   script.py (View)       │  pushbutton entry point
                        │  loads MainWindow.xaml    │
                        └───────────┬──────────────┘
                                    │ DataContext
                                    ▼
                        ┌─────────────────────────┐
                        │   MainViewModel           │  MVVM (INotifyPropertyChanged
                        │   (viewmodels/)            │  + ICommand via RelayCommand)
                        └───────────┬──────────────┘
                     orchestrates   │   reads/writes plain models
              ┌─────────────────────┼─────────────────────────┐
              ▼                     ▼                          ▼
   ┌────────────────────┐ ┌──────────────────────┐  ┌───────────────────────┐
   │ balancing_service    │ │ arrangement_service    │  │ spare_space_service    │
   │ (pure Python)         │ │ (pure Python)           │  │ (pure Python)           │
   └──────────┬──────────┘ └──────────┬───────────┘  └───────────┬────────────┘
              └─────────────┬─────────┴───────────────────────────┘
                             ▼
                  ┌────────────────────────┐
                  │   schedule_service.py     │  orchestration + Revit writes
                  └───────────┬────────────┘
                              ▼
                  ┌────────────────────────┐
                  │   revit_utils.py          │  ONLY module touching
                  │  (Revit API boundary)     │  Autodesk.Revit.DB / .Electrical
                  └────────────────────────┘
                              ▼
                        Revit Document
                     (Panels, ElectricalSystems,
                      PanelScheduleViews)
```

**Key architectural decision:** business logic (balancing math, R‑Y‑B slot
planning, spare/space percentage math) is implemented in plain Python with
**zero Revit API imports** (`models.py`, `services/balancing_service.py`,
`services/arrangement_service.py`, `services/spare_space_service.py`,
`services/report_service.py`). Only `revit_utils.py` imports
`Autodesk.Revit.DB*`. This means:

- The core algorithms can be unit tested with plain CPython/pytest, outside
  Revit, in CI.
- The Revit-facing code stays a thin, defensive translation layer that is
  easy to review for transaction safety and API-version sensitivity.
- Swapping UI frameworks (e.g. porting to a CPython/pyRevit 5 engine or a
  different host) only requires rewriting the view + ViewModel glue, not the
  engineering logic.

### Data flow (per batch run)

1. **Select Panels (Step 1):** `revit_utils.get_all_electrical_panels` /
   `selected_panels_from_selection` finds `OST_ElectricalEquipment` elements
   that expose panel schedule capability. Each is converted to a
   Revit-free `models.PanelInfo` (+ `models.CircuitInfo` per assigned
   `ElectricalSystem`) via `revit_utils.build_panel_info`.
2. **Configure Balancing / Spare-Space (Steps 2–3):** the ViewModel binds
   directly to `models.BalancingSettings` / `models.SpareSpaceSettings`.
3. **Preview (Step 4):** `schedule_service.run_preview` clones each
   `PanelInfo`, runs `balancing_service.balance_panel` +
   `arrangement_service.build_slot_plan` +
   `spare_space_service.plan_spare_space` **entirely in memory**, and
   returns before/after numbers. **No Revit transaction is opened for the
   preview** — this is what lets Step 4 be instant and risk-free.
4. **Execute (Step 5):** `schedule_service.execute_batch` processes each
   panel in its own Transaction, in this exact order (per requirement):
   **(a) generate/ensure the panel schedule exists first** via
   `revit_utils.create_or_get_panel_schedule_view`, and confirm its real
   slot count is readable — nothing else happens for that panel until
   this succeeds; **(b) then** run `balancing_service.balance_panel`;
   **(c) then** `arrangement_service.build_slot_plan` for R-Y-B ordering;
   **(d) then** fill spare/space via `revit_utils.fill_spare_and_space`
   (with breaker rating). All four steps for one panel run inside a
   single outer `TransactionGroup` so the whole batch is one Undo step —
   but each panel is wrapped in its own try/except so one panel's failure
   never rolls back panels already completed earlier in the same batch
   (see section 8's notes on this).
5. **Report:** `report_service.build_report_rows` turns the
   `BalancingResult` + `SpareSpaceReport` objects into grid rows and can
   export HTML/CSV.

---

## 2. Folder Structure

### 1a. Spare/Space with Breaker Rating (Ways Table mode)

Step 3 now offers two modes:

- **Standard Ways Table (default)** - ported from a proven, previously
  working pyRevit extension (SpareSpaceFiller) that a colleague shared.
  Given a panel's actual **connected circuit count**, it rounds UP to the
  smallest standard panel size in an editable bracket table (12/18/24/30/
  36/42/48/54-way by default) and uses that bracket's fixed Spare/Space
  quota - not a flat percentage. It also looks up the **spare breaker
  rating and pole count** from an editable panel-name-prefix table (e.g.
  panels named `ACP-...` default to 30A/2-pole; `PP`/`LP`/`ELP` to 20A/
  1-pole; `UDB` to 30A/1-pole), matching the longest prefix first, with a
  20A/1-pole fallback for unmatched names. If the panel doesn't have enough
  capacity for the connected circuits plus the planned spare/space, its
  capacity parameter is increased automatically (and the schedule
  re-checked afterward, since some schedule templates are configured with
  a *fixed* row count that won't grow even if the parameter changes - this
  is detected and logged rather than silently under-filling). Multi-pole
  spares/spaces are placed on genuinely consecutive physical rows in the
  same column, phase-aligned to start on "R" when possible.
- **Percentage of capacity** - the simpler flat-percentage mode this tool
  originally shipped with, kept for panels/firms that don't use a fixed
  set of standard sizes. No breaker rating is assigned in this mode.

Both tables in Ways Table mode are directly editable in the wizard (add
rows with **+ Add row**) and persist across sessions the same way other
settings do.

### The API correction this feature exposed

Cross-checking against that proven prior extension revealed that
`PanelScheduleView.AddSpare` / `AddSpace` do **not** take a linear slot
number at all - they take a **(row, column)** cell address, exactly as
returned by `GetCellsBySlotNumber(slotNumber)`. Earlier revisions of this
tool passed a raw slot number directly, which only "worked" by coincidence
for low slot numbers / single-column layouts and produced the "row number
nRow is invalid in Body" error reported during testing. Every function
that touches a slot now resolves `slot -> (row, col)` first. Likewise,
"is this slot empty" is now checked via `GetCircuitByCell(row, col)`
returning `None`, and the schedule's true slot count is read directly via
`GetTableData().NumberOfSlots` - both confirmed against the same proven
implementation rather than second-hand documentation. See the "THIRD
CORRECTION" note in `revit_utils.py`'s module docstring for the full
account.



```
PanelScheduleManager.extension/
│
├── extension.json                          # pyRevit extension manifest
│
├── PanelTools.tab/
│   └── Electrical.panel/
│       └── PanelScheduleManager.pushbutton/
│           ├── bundle.yaml                 # button title/tooltip/context
│           └── script.py                   # entry point (View)
│
├── lib/
│   └── panelmgr/                           # shared library (importable package)
│       ├── __init__.py
│       ├── config.py                       # settings persistence (JSON)
│       ├── logger.py                       # logging setup
│       ├── models.py                       # Revit-free data classes
│       ├── revit_utils.py                  # ONLY Revit API boundary
│       │
│       ├── services/
│       │   ├── __init__.py
│       │   ├── balancing_service.py        # load balancing (pure Python)
│       │   ├── arrangement_service.py      # R-Y-B slot planning (pure Python)
│       │   ├── spare_space_service.py      # spare/space planning (pure Python)
│       │   ├── schedule_service.py         # batch orchestration (Revit writes)
│       │   └── report_service.py           # report rows + CSV/HTML export
│       │
│       ├── viewmodels/
│       │   ├── __init__.py
│       │   ├── mvvm_base.py                # ObservableObject, RelayCommand
│       │   ├── panel_item_viewmodel.py     # grid row VMs
│       │   └── main_viewmodel.py           # wizard ViewModel
│       │
│       ├── views/
│       │   └── MainWindow.xaml             # 5-step wizard UI
│       │
│       └── resources/                      # (reserved for shared XAML styles)
│
├── tests/
│   └── test_services.py                    # pytest suite, pure Python
│
└── README.md                               # this file
```

---

## 3. Revit API Workflow Explanation

| Step | Revit API surface used | Notes |
|---|---|---|
| Discover panels | `FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_ElectricalEquipment)` | Filters to elements whose `MEPModel` exposes panel-schedule capability (presence of a "Distribution System" parameter). |
| Read circuits | `FamilyInstance.MEPModel.AssignedElectricalSystems` → `Autodesk.Revit.DB.Electrical.ElectricalSystem` | Reads `BuiltInParameter.RBS_ELEC_CIRCUIT_NUMBER`, `RBS_ELEC_CIRCUIT_NAME`, `RBS_ELEC_APPARENT_LOAD`, `PolesNumber`. |
| Create schedule | `PanelScheduleView.CreateInstanceView(doc, panelId)` (project default) **or** `CreateInstanceView(doc, panelId, templateId)` (specific `PanelScheduleTemplate`) | Idempotent — first checks `FilteredElementCollector(doc).OfClass(PanelScheduleView)` for an existing view bound to the panel via `view.GetPanel()`. **Which template gets used is now a Step 1 UI choice** — see section 4a below. |
| Renumber / rearrange | `ElectricalSystem.get_Parameter(RBS_ELEC_CIRCUIT_NUMBER).Set(...)` **or** `PanelScheduleView.CanMoveSlotTo(fromSlot, fromSeq, toSlot, toSeq)` + `MoveSlotTo(...)` | **Important limitation:** whether the circuit number parameter is writable depends on the panel schedule template's numbering mode (Manual vs Automatic). `revit_utils.try_set_circuit_number` attempts the direct write first; if the parameter is read-only, `schedule_service._apply_slot_plan` falls back to `MoveSlotTo`. **A further, documented API limitation:** `MoveSlotTo` refuses to move a circuit into an already-occupied destination slot (there is no atomic swap call) — `CanMoveSlotTo` is checked first and a failed move is logged as a warning rather than aborting the batch. `arrangement_service.build_slot_plan` mitigates this by planning against currently-free slots wherever possible. |
| Fill spare/space | `PanelScheduleView.AddSpare(slot, slotSequence)` / `AddSpace(slot, slotSequence)` | Only called on slots confirmed empty via `IsSpare` / `IsSpace` plus the tool's own record of which slots its circuits occupy (`revit_utils.get_empty_slots`) — there is no single "IsSlotEmpty" method in the real API. `slotSequence` is the pole position within the slot; this tool only creates single-pole spares/spaces, so it is always `1`. |
| Transaction safety | `TransactionGroup` (whole batch) wrapping one `Transaction` per panel | Gives the user a single Undo step and lets **Cancel** roll back the entire group cleanly, including any panels already processed earlier in the same run. |

### Why the UI runs modal (`ShowDialog`), not modeless

### 3a. Which panel schedule template gets used

Revit's `PanelScheduleView.CreateInstanceView` has two overloads: one that
uses whatever is configured as the **default** panel schedule template for
a panel's distribution system (Electrical Settings → Panel Schedule
Templates in the Revit UI — a project-level setting the tool does not
control), and one that takes an explicit `PanelScheduleTemplate` element
id. **Step 1 of the wizard now exposes a "Panel schedule template"
dropdown**, populated by enumerating every `PanelScheduleTemplate` in the
current project (`revit_utils.get_panel_schedule_templates`), with a
leading **"Project Default"** entry that falls back to the 2-argument
overload. Your selection is remembered across sessions the same way the
balancing/spare-space settings are. This only affects **newly created**
schedules — panels that already have a `PanelScheduleView` keep it as-is
regardless of what's selected here.

Revit's API is single-threaded: every element read/write must happen on
Revit's own UI thread inside an open `Transaction`. A modeless WPF window
normally requires an `IExternalEventHandler` + `ExternalEvent` pair to safely
raise a request back onto Revit's thread. Because this tool's workflow is a
**linear wizard that blocks on user input between Revit-touching steps**
anyway (Select → Configure → Configure → Preview → Execute), there is no
benefit to modeless operation, and modal `ShowDialog()` keeps the
implementation simpler and eliminates an entire class of cross-thread bugs.
Progress bar responsiveness during the Step 5 batch is achieved by invoking
`Dispatcher.Invoke(..., DispatcherPriority.Background)` between panels — see
`main_viewmodel.py`, `_run_on_ui`.

---

## 4. WPF UI Design

The interface follows a DiRoots-style layout: a **dark left rail** with the
tool name and a 5-item step indicator, and a **light content card** on the
right that swaps per step. Design tokens:

- Accent: `#2563EB` (blue) / hover `#1D4ED8`
- Sidebar: `#111827` (near-black) with `#9CA3AF` inactive text
- Cards: white, `10px` corner radius, `1px #E5E7EB` border
- Typography: Segoe UI, 18px semi-bold section titles, 13px secondary text

Step switching is implemented **without a converter class** by exposing
five `VisibilityX` properties on `MainViewModel` (`Step1Visibility` …
`Step5Visibility`) that return `Visibility.Visible` / `Collapsed` based on
`CurrentStep`, avoiding the need to register an `IValueConverter` in XAML
(IronPython classes can implement `IValueConverter`, but keeping it as plain
properties is simpler to maintain here).

Interface requirements mapped to concrete controls:

| Requirement | Implementation |
|---|---|
| Progress bar | `ProgressBar` bound to `ProgressValue` / `ProgressMax`, Step 5 |
| Status messages | `TextBlock` bound to `StatusMessage`, updated every panel via `Dispatcher.Invoke` |
| Error handling | Every command handler wraps logic in `try/except`, logs via `panelmgr.logger`, and surfaces a friendly `StatusMessage` / `pyrevit.forms.alert` |
| Cancel button | `CancelCommand` sets a flag checked between panels in `schedule_service.execute_batch`; on cancel the whole `TransactionGroup` is rolled back |
| Settings persistence | `config.py` reads/writes `%APPDATA%\pyRevit\PanelScheduleManager\settings.json`, loaded on `MainViewModel.__init__`, saved after a successful batch |
| Preview grid | `DataGrid` bound to `PreviewRows` (Step 4) |
| Results summary | Green summary banner (`ResultsSummary`) + `DataGrid` bound to `ResultRows` (Step 5) |

---

## 5. Step-by-Step Implementation Guide

1. **Install pyRevit** (5.x or 4.8+) if not already present, and confirm
   Revit 2025 is a supported host in your pyRevit build.
2. **Copy the extension.** Place the entire
   `PanelScheduleManager.extension` folder into your pyRevit extensions
   directory, e.g.
   `%APPDATA%\pyRevit\Extensions\PanelScheduleManager.extension`, or add its
   parent folder as a custom extension search path via
   `pyRevit → Settings → Custom Extension Directories`.
3. **Reload pyRevit** (`pyRevit → Reload`, or restart Revit). A new
   **Panel Tools** tab appears with an **Electrical** panel and the
   **Panel Schedule Manager** button.
4. **Open a Revit 2025 project** containing electrical panels
   (`OST_ElectricalEquipment` family instances with assigned
   `ElectricalSystem`s / branch circuits).
5. **Click the button.** `script.py` resolves `revit.doc` / `revit.uidoc`,
   constructs `MainViewModel(doc, uidoc)`, loads `MainWindow.xaml`, wires
   `window.DataContext = view_model`, and shows it modally.
6. **Step 1 – Select Panels:** the grid loads from your **current Revit
   selection** by default when you open the tool - select the panels you
   want (Project Browser, a view, or the panel elements themselves)
   *before* clicking the pushbutton, or click **"Use Current Selection"**
   to reload after changing it. **"Scan All Panels"** is a separate,
   explicit, opt-in action for browsing every panel in the model instead -
   it's no longer run automatically, since a full-model scan can be slow
   on large projects and usually isn't what's actually wanted. "Select
   All"/"Select None" are convenience commands on top of whichever list is
   currently loaded.
7. **Step 2 – Balancing Rules:** toggle balancing on/off, set the target
   max imbalance %, decide whether locked circuits are respected, and
   whether R‑Y‑B arrangement runs after balancing.
8. **Step 3 – Spare & Space:** set spare %, space %, reserved slot count,
   and whether leftover slots auto-fill as Space.
9. **Step 4 – Preview:** clicking **Next** from Step 3 triggers
   `_compute_preview()`, which runs the full algorithm stack **in memory**
   and populates the before/after grid. No Revit changes yet.
10. **Step 5 – Execute:** click **Run** to call `schedule_service
    .execute_batch`, which opens the `TransactionGroup`, processes each
    panel, and reports progress/status live. **Cancel** at any point rolls
    back everything written so far. On success, a results grid and green
    summary banner appear, and **Export Report** writes an HTML report via
    `report_service.export_html`.
11. **Settings persist automatically** — reopening the tool in a later
    session restores your last balancing/spare-space configuration.

---

## 6. Testing Strategy

### 6.1 Unit tests (pure Python, no Revit required)

`tests/test_services.py` covers `balancing_service`, `arrangement_service`,
and `spare_space_service` using fixture `PanelInfo`/`CircuitInfo` objects.
Run locally with:

```bash
cd PanelScheduleManager.extension
python -m pytest tests/ -v
```

All 11 tests pass against the shipped implementation (verified during
development):

- Balancing reduces or maintains imbalance; never makes it worse.
- Multi-pole (2/3-pole) circuits are never split across a rebalance.
- Locked circuits are never moved.
- Disabling balancing is a true no-op.
- Zero-load panels don't divide-by-zero.
- Slot plans never assign the same slot twice (`validate_no_duplicate_slots`).
- 3-pole circuits always land on a clean R‑Y‑B triplet.
- Locked circuits keep their pinned slot.
- Spare/space percentages are computed against total panel capacity and
  floored, matching common spec-section conventions.
- `auto_fill_remaining` consumes every leftover empty slot as Space.
- Reserved slots are excluded from both spare and space pools.

### 6.2 Integration testing (inside Revit)

Because `revit_utils.py`, the WPF window, and the ViewModel's
Dispatcher-based progress updates all require a live Revit + pyRevit host,
these are validated manually against representative test models:

- A **single-phase, split-bus residential panel** (small slot count, mixed
  1-pole loads).
- A **three-phase 42-slot panel** with a mix of 1-pole, 2-pole and 3-pole
  circuits, including at least one heavily loaded 3-pole motor circuit.
- A **panel that already has a PanelScheduleView** (tests the "get or
  create" idempotency path).
- A **panel with zero existing circuits** (tests that spare/space fill
  alone works without balancing having anything to do).
- **Cancel mid-batch** on a 10+ panel selection, confirming the
  `TransactionGroup` rollback leaves the model byte-for-byte unchanged
  (checked via `Document.IsModified` and a before/after warning count).

### 6.3 Regression checklist before each release

- [ ] `pytest tests/` green
- [ ] Extension loads with no pyRevit console errors on a clean Revit 2025
      + current pyRevit build
- [ ] Panel discovery finds panels in a project with mixed 1φ/3φ equipment
- [ ] Preview numbers match Execute results exactly (same algorithm path)
- [ ] Cancel during Execute leaves 0 net changes in the model
- [ ] Settings round-trip across a Revit restart
- [ ] Exported HTML report opens correctly and matches the results grid

---

## 7. Deployment Instructions

### Option A — Manual copy (single machine / testing)

1. Copy `PanelScheduleManager.extension` to
   `%APPDATA%\pyRevit\Extensions\`.
2. `pyRevit → Reload`.

### Option B — Firm-wide deployment via a Git extension source

1. Push this folder to a Git repository (e.g.
   `git@yourfirm.git:revit-tools/PanelScheduleManager.extension.git`).
2. On each workstation (or via a login/deployment script), run:
   ```bash
   pyrevit extend ui PanelScheduleManager https://your-repo-url.git
   ```
   or add the repo URL under
   `pyRevit → Settings → Extensions → Add from Git Repo` in the pyRevit UI.
3. `pyRevit → Reload`. Future updates are pulled with `pyRevit → Update`.

### Option C — Centralized `%PROGRAMDATA%` install (locked-down environments)

1. Place the extension under
   `%PROGRAMDATA%\pyRevit\Extensions\PanelScheduleManager.extension`.
2. Ensure the pyRevit config (`pyRevit_config.ini` or the equivalent
   `%APPDATA%\pyRevit\pyRevit_config.ini`) lists this path under
   `userextensions` or is included via the admin-deployed pyRevit config,
   so it is picked up for all users without write access to `%APPDATA%`.

### Versioning & rollback

- Bump `extension.json → version` on every release.
- Settings persistence (`config.py`) is additive/backward-compatible via
  `_deep_merge` against `_DEFAULTS`, so older `settings.json` files from a
  prior version continue to load without error after an update.
- Because the tool wraps its entire batch in one `TransactionGroup`, a bad
  run can always be undone with a single **Ctrl+Z** in Revit even without
  reopening the tool.

---

## 8. Known Limitations / Notes for Production Hardening

- **Schedule row count vs. family "Slots" parameter**: the panel family's
  "Slots" / "Number of Poles/Slots" instance parameter is only a nominal
  hint. The panel schedule's actual writable capacity is whatever
  `PanelScheduleView.GetTableData().GetSectionData(SectionType.Body)
  .NumberOfRows` reports, and the two are not guaranteed to match (this was
  found from a live traceback: `AddSpace` raised "The given row number nRow
  is invalid in Body" past the schedule's real row count). The tool now
  reconciles against the real row count — read-only at discovery/preview
  time for panels with an existing schedule, and immediately after creation
  for panels that don't have one yet — and clamps to whichever is smaller,
  logging a warning if they disagree. If your firm's panel schedule
  templates are configured with fewer rows than a panel's physical pole
  capacity by design (e.g. templates that only grow rows as circuits are
  added), the effective spare/space/arrangement capacity this tool works
  within will be the template's row count, not the enclosure's true
  capacity — check the warning log if numbers look lower than expected.
- **Circuit renumbering write-access** depends on the panel schedule
  template's numbering mode; see the workflow table in section 3. Firms
  using strictly "Automatic" numbering should validate the
  `MoveSlotTo`/`CanMoveSlotTo` fallback against their specific panel
  schedule templates, and note that Revit will not move a circuit into an
  already-occupied destination slot (no atomic swap exists in the API) —
  `arrangement_service.build_slot_plan` plans against free slots for this
  reason, but a fully-packed panel with no free slots to stage through may
  still leave some circuits unmoved, logged as warnings rather than errors.
- **`PanelScheduleView.CreateInstanceView` can return `None` instead of
  raising** — observed live when a panel's distribution system has no
  compatible default template (or the explicitly-selected Step 1 template
  doesn't match that panel's phase/voltage config). `revit_utils
  .create_or_get_panel_schedule_view` now checks for this explicitly,
  falls back to the project default template if a specific selection
  failed, and raises a clear, actionable error (not a raw
  `AttributeError`) if it still can't build a schedule — telling you to
  assign a default template for that distribution system in Electrical
  Settings, or pick a compatible one in Step 1.
- **A speculative `doc.Regenerate()` retry crashed Revit itself.** An
  earlier revision called `Document.Regenerate()` as a retry whenever
  `PanelScheduleView.GetTableData()` returned `None` during read-only
  panel scanning (Step 1 discovery), on the assumption that `Regenerate()`
  is always safe outside a Transaction. On at least one real model this
  triggered `Failed to regenerate` and took the entire Revit process down
  - not a normal, catchable .NET exception, an actual application crash
  that no try/except could have protected against. This has been removed
  entirely: `get_total_slots` / `get_schedule_body_row_count` no longer
  call `Regenerate()` under any circumstances during read paths. The only
  `Regenerate()` call left in the codebase is inside `bump_panel_capacity`,
  which is structurally different and much lower-risk - it runs inside an
  active `Transaction` during Execute, is called at most once per panel
  that actually needs its capacity increased, and directly follows the
  parameter change it needs to observe (the standard, required Revit API
  pattern of change-then-regenerate), rather than being a speculative fix
  attempt on a passive read. If a panel's slot count still can't be read,
  the tool now logs an ERROR and treats that panel as needing manual
  attention (a manual Regenerate/reload via Revit's own UI, where the user
  can see and respond to any warning dialogs) rather than trying to force
  a fix automatically.
- **Even after removing that `Regenerate()` call, Revit crashed again on
  the same class of broken panel schedule** - this time with no further
  Python traceback possible at all (confirming it was a native crash, not
  something our code could catch), occurring immediately after the "0
  slots after every fallback" warnings for that panel. The cause: the
  tool was proceeding anyway with a nominal/guessed slot count and
  continuing to call MORE Revit API methods (`GetCellsBySlotNumber`,
  `GetCircuitByCell`, `AddSpare`, `MoveSlotTo`, etc.) against a
  `PanelScheduleView` that had already failed to answer the most basic
  read call (`GetTableData()`) - repeatedly poking at an object that's
  already signaling it's broken is itself what destabilized Revit. The
  fix: `execute_batch` now treats an unreadable slot count as an
  IMMEDIATE, hard stop for that one panel - it raises before any further
  Revit API calls are made against that schedule, which is caught by the
  per-panel try/except described below (only that panel's own transaction
  rolls back; the rest of the batch is unaffected and untouched by any
  further calls against the broken object). A panel schedule that can't
  report its own slot count should be treated as a data-integrity problem
  to fix manually in Revit, never as something to guess past.
- **One panel's failure no longer rolls back the whole batch.** Earlier
  versions wrapped every panel's work in a single outer `TransactionGroup`
  with no per-panel exception handling, so an exception on panel N (e.g.
  the template issue above) propagated out of the loop and rolled back
  every panel already successfully processed before it — confirmed by a
  live run where 4 panels succeeded, panel 5 failed, and all 5 were
  discarded. `schedule_service.execute_batch` now isolates each panel in
  its own try/except: a failure rolls back only that panel's own
  `RevitTransaction` and is recorded in a `failed_panels` list (surfaced
  in the Step 5 results grid with a FAILED status and error message),
  while the rest of the batch continues. Only an explicit user Cancel
  still rolls back the entire `TransactionGroup`.
- **Step 1 no longer scans the whole model automatically.** Earlier
  versions called `get_all_electrical_panels` (a full `FilteredElementCollector`
  over every `OST_ElectricalEquipment` element, building a `PanelInfo` for
  each) automatically on startup and on every "Refresh" click - slow on
  large projects and rarely what was actually wanted, since the user
  usually already knows which panels to work on. The default is now to
  load from whatever is already selected in Revit
  (`revit_utils.selected_panels_from_selection`) when the tool opens;
  "Scan All Panels" remains available as a separate, explicit, opt-in
  button for the cases where browsing the entire model is genuinely
  wanted.
- **Every panel's nominal slot-count guess showed 42, regardless of the
  panel's real size.** `build_panel_info`'s initial guess only checked two
  parameter names ("Slots", "Number of Poles/Slots") before defaulting to a
  hardcoded `42`; on a real project none of the panel families used either
  name (they used `Max Number of Single Pole Breakers`, already known to
  `bump_panel_capacity` for a completely different purpose), so every
  panel - 12-way, 18-way, 24-way, 60-way alike - silently fell through to
  42, producing a wall of "nominal slot count (42) does not match..."
  warnings and a wrong "Slots" column in the Step 1 panel picker. This was
  harmless for final results (the reconciliation step already corrects it
  against the schedule's real slot count wherever one exists) but noisy
  and misleading before that correction kicks in. Fixed by trying every
  known capacity parameter name (`NOMINAL_SLOT_PARAM_NAMES`, which
  includes `CAPACITY_PARAM_NAMES` plus the two generic fallback names) for
  the read-only guess, while keeping `bump_panel_capacity`'s WRITE target
  restricted to the narrower `CAPACITY_PARAM_NAMES` list only - a broad
  guess is safe to read, but not safe to write to, since a name like
  "Slots" could be an unrelated custom parameter on some families.
- **Step 5's results grid showed Python internals instead of real data.**
  It used `AutoGenerateColumns="True"` on a `DataGrid` bound to dynamically-
  typed IronPython objects (`ResultRowViewModel`, which sets attributes via
  `setattr` at construction rather than declaring CLR properties). WPF's
  automatic column generation relies on CLR reflection to discover
  properties and, unable to find real ones on a dynamic Python object,
  fell back to whatever it COULD find - Python's dunder attributes
  (`__class__`, `__delattr__`, `__dict__`, `__doc__`), shown as columns
  literally named `_class_`, `_delattr_`, `_dict_`, `_doc_`. Every other
  grid in the wizard already used explicit `DataGrid.Columns` (the shared
  style even sets `AutoGenerateColumns="False"` by default); this one grid
  had explicitly opted back into auto-generation. Fixed by giving it
  explicit columns like the rest, including a `Status` column (now
  populated as `"OK"` for successful panels too, not just the
  `FAILED: <reason>` text for skipped ones), so the full per-panel error
  text is actually visible instead of being hidden behind reflection noise.
- **A freshly created panel schedule's slot count was unreadable, and
  "MEE_Distribution Board" turned out to be a red herring.** A closer look
  at a live run revealed that the schedule reported as unreadable
  ("MEE_Distribution Board") wasn't a separate, mysteriously broken panel
  at all - it was simply the VIEW NAME that a specific firm template gives
  every schedule created from it, inherited by whichever panel is
  currently being processed (in this case `LP-CC-N-B1-03-01N`). The real
  cause: `GetTableData()` returns `None` on a schedule immediately after
  `CreateInstanceView` creates it, until the document is regenerated once.
  This is a DIFFERENT situation from the FOURTH/FIFTH corrections above -
  those were about a *speculative* `Regenerate()` retry crashing Revit
  when reading an *existing*, possibly-corrupted schedule *outside* any
  transaction. This is the standard, deterministic, safe pattern instead:
  call `Regenerate()` exactly once, unconditionally, immediately after
  creating a brand new view, always from inside an active `Transaction`
  (the same category of call `bump_panel_capacity` already used safely).
  `create_or_get_panel_schedule_view` now does this for the
  newly-created-view branch only; the "schedule already existed" branch
  is unaffected and still never calls `Regenerate()`.
- **"Failed to regenerate" recurred - this time caught cleanly, and it's
  now clear this is a genuine document problem, not a code problem.** The
  same underlying Revit error crashed the whole application on one panel
  (FOURTH correction, a passive read outside any transaction) and was
  caught gracefully as a normal exception on another panel (this
  correction, the deterministic, transaction-scoped post-creation
  `Regenerate()` from the SEVENTH correction above). Seeing the identical
  failure from both a risky call and a standard, safe one is strong
  evidence the problem isn't how or when this tool calls `Regenerate()` -
  specific panels in this project have a genuine data-integrity issue that
  no code-level retry or restructuring can safely work around. The
  post-creation `Regenerate()` call is now wrapped with an explicit,
  actionable error message recommending a Revit-side **Audit** (File ->
  Open, check "Audit") before reprocessing an affected panel, rather than
  a generic failure message. If a panel keeps failing here after an audit,
  that panel's underlying family instance or distribution system likely
  needs to be rebuilt from scratch in Revit.
- **"Failed to regenerate" hit EVERY new-schedule-creation attempt, not
  just one panel - 100% reproducible, confirmed directly.** After the
  EIGHTH correction's explicit `doc.Regenerate()` call (deterministic,
  transaction-scoped, the standard-pattern approach) went on to fail on
  every single subsequent panel needing a brand-new schedule across
  multiple different panel types, that pattern ruled out isolated
  per-panel corruption as the sole explanation - the explicit
  mid-transaction `Regenerate()` call itself appears to be the unreliable
  part in this project. The fix: schedule creation now runs in its OWN
  `Transaction` that commits immediately, relying on `Transaction.Commit
  ()`'s IMPLICIT regeneration - a different, more mature Revit API code
  path - instead of an explicit mid-transaction call. **Important tradeoff
  this introduces:** because schedule creation now commits on its own, a
  panel that fails at any LATER step (slot-count reconciliation,
  balancing, arrangement, spare/space) keeps its newly-created schedule
  even though the panel is reported as FAILED overall - only work after
  schedule creation rolls back, not the schedule itself. This is
  considered acceptable (the schedule is a genuine, useful partial result,
  and re-running later finds it via the "already exists" path rather than
  needing to recreate it) but is a real behavior change from the earlier,
  fully-atomic single-transaction design, and both the code comments and
  the failure message shown to the user call this out explicitly. If this
  new approach ALSO fails 100% of the time, that is very strong evidence
  the issue is not code-side at all - the definitive test is attempting to
  manually create a schedule for the same panel via Revit's own native UI
  (Systems tab -> Panel Schedules -> New); if that also fails, only a
  Revit-side Audit (or deeper Autodesk-level investigation) can resolve
  it, not further changes to this tool.
- **Switchboard-style panels showed "nominal slot count (0)".** The
  nominal-guess loop (see the SIXTH correction above) stopped at the first
  candidate parameter name that merely EXISTED, even if its value was `0`
  - which happens when a panelboard-specific parameter (`Max Number of
  Single Pole Breakers`) is also present but unused/zero on a
  switchboard-style family whose real capacity is actually stored under a
  different name later in the list (`Max Number of Circuits`). A live run
  on 60-way switchboards showed exactly this. Fixed to only accept and
  stop on a STRICTLY POSITIVE value, continuing through the remaining
  candidate names otherwise, falling back to the original `42` placeholder
  (not `0`) only if none of them ever produce a positive number.
- **Checking a panel's checkbox in Step 1 didn't enable the Next button.**
  `RelayCommand` (this tool's own `ICommand` implementation - see
  `mvvm_base.py`) only re-queries `CanExecute` when explicitly told to via
  `raise_can_execute_changed()`; it does not use WPF's built-in
  `CommandManager.RequerySuggested` auto-requery the way native .NET
  commands do. `PanelItemViewModel.IsSelected`'s setter notified WPF that
  the checkbox's own visual state changed, but nothing told `NextCommand`
  to re-check whether it should now be enabled - so after checking a
  panel, Next stayed visually and functionally disabled even though a
  valid selection existed. Fixed by giving `PanelItemViewModel` an
  `on_selection_changed` callback (wired to `MainViewModel
  ._refresh_can_execute` in `_populate_panels`), invoked every time
  `IsSelected` actually changes, so ticking or unticking any panel
  immediately re-evaluates Next/Back/Run/Export's enabled state.
- **Panels weren't being "right-sized" down - a panel with 2 connected
  circuits stayed at its starting 14-way capacity instead of shrinking to
  the 12-way bracket.** `bump_panel_capacity` only ever GREW capacity
  (`if new_capacity <= current_slots: return current_slots` was a
  guaranteed no-op whenever the target bracket was smaller than the
  panel's current/default capacity), confirmed live: a lightly-loaded
  panel that should have shrunk to the smallest sufficient standard
  bracket per its connected circuits + spare + space stayed at whatever
  larger capacity the family/template started it at. Fixed to support
  shrinking too, with a hard safety guard
  (`_get_highest_occupied_slot`): capacity is NEVER reduced below the
  highest slot currently occupied by a REAL connected circuit (existing
  Spare/Space placeholders don't count, since they're exactly what this
  same operation is about to redefine), so a shrink can never destroy
  real circuit data. The capacity-parameter `Regenerate()` call inside
  this function - the one remaining explicit mid-transaction
  `Regenerate()` call left in the codebase, since there's no safe
  "commit and rely on implicit regeneration" alternative available
  mid-placement-loop here - is now also wrapped with a clear, specific
  error message rather than a bare call, in case it hits the same
  category of failure documented in the TENTH correction above.
- **"Load balance not working" - loads stayed just as imbalanced after a
  successful run.** Two compounding bugs, found together from a live
  printed schedule showing a heavily lopsided panel (R=8870/Y=900/B=2749)
  after a "successful" balance:
  1. `CircuitInfo.slot` was never actually populated from real Revit data
     anywhere in the codebase - it stayed at its default `None` for every
     circuit, meaning `swap_schedule_slots` never had a valid "from"
     position to move a circuit FROM, and arrangement's locked-circuit
     handling was silently skipped every time. Fixed with a new
     `revit_utils.get_circuit_slot_map`, which scans the schedule via
     `GetCircuitByCell` and matches circuits back by `ElementId`, called
     both at Step 1 discovery (for existing schedules) and right after
     schedule creation/reconciliation at Execute time (for both existing
     and newly-created schedules).
  2. `_apply_slot_plan` tried `try_set_circuit_number` (writing the
     displayed "Circuit Number" label) FIRST, only falling back to a true
     physical slot move if that failed. Since a circuit's real phase
     connection is determined by its PHYSICAL SLOT, not by this label,
     the write could succeed (no exception, not read-only) while doing
     nothing to actually reconnect the circuit to a different phase - a
     false positive that looked like success while changing nothing.
     `_apply_slot_plan` now uses `swap_schedule_slots` (MoveSlotTo)
     exclusively; `try_set_circuit_number` is no longer called from this
     path and its docstring now warns against reuse for this purpose.
  **Known residual limitation:** because `MoveSlotTo` has no atomic swap
  (a destination must be free to move into - see the arrangement-service
  notes above), a batch of circuits whose target slots form a circular
  dependency (A needs where B currently is, and vice versa) can still
  leave some moves only partially applied within one run, logged as
  warnings rather than silently failing; re-running after a partial move
  will often resolve the rest since more slots are now free.
- **Load balance STILL not applying, even after the TWELFTH correction -
  root cause was a gridlocked panel, not stale code.** A before/after
  comparison on a nearly-fully-packed panel (almost every slot already
  holding a real circuit) showed per-phase totals completely unchanged.
  Cause: `MoveSlotTo` has no atomic swap - a destination must already be
  free - so a single pass of direct moves gridlocks completely whenever
  every candidate destination is occupied by ANOTHER circuit that ALSO
  needs to move, which is exactly what happens on a panel with little to
  no free capacity to begin with. Fixed with a proper multi-pass +
  staging-slot resolver (`arrangement_service.resolve_moves_with_staging`,
  extracted as pure Python specifically so it could be unit tested in
  isolation - see `tests/test_services.py`'s resolver tests covering
  simple chains, 2-cycles, longer cycles, and true gridlock with zero
  free slots): each pass executes every move whose destination happens to
  be free (a pool that grows as moves land); if a full pass makes zero
  progress, one pending circuit is staged into any genuinely free slot in
  the panel to break the deadlock and re-queued to reach its real target
  on a later pass. If truly no free slot exists anywhere, this is logged
  clearly rather than silently giving up or looping forever - increasing
  the panel's capacity before arrangement (see the ELEVENTH correction's
  right-sizing) is the fix in that case. This also directly addresses
  wanting all Spare/Space grouped at the end rather than scattered: the
  existing per-phase packing logic already fills real circuits into the
  lowest available slots first and leaves the highest for spare/space -
  it simply couldn't take effect while arrangement was silently
  gridlocking on packed panels.
- **Balancing saw ZERO circuits for a panel with real, substantial
  loads.** A live run's results grid showed Total Load=0, R=0, Y=0, B=0,
  Circuits Moved=0 for a panel whose printed schedule clearly showed
  37,760W of real connected load - yet spare/space filling worked
  correctly on the same panel (5 spares, 6 spaces added). Cause: two
  completely independent circuit-discovery mechanisms existed in the
  codebase - `panel_info.circuits` (used for balancing) came solely from
  `panel_element.MEPModel.AssignedElectricalSystems`, while spare/space's
  "connected" count came from a separate schedule-based scan
  (`count_connected_ways`, using `GetCircuitByCell`). For this panel,
  `AssignedElectricalSystems` returned empty/incomplete while the
  schedule-based scan correctly found real content. Fixed with
  `revit_utils.reconcile_circuits_with_schedule`: cross-checks the two
  sources, and for any circuit the schedule scan found that
  `AssignedElectricalSystems` missed, attempts to read it directly and
  add it to `panel_info.circuits` so balancing can actually see and move
  it. If a specific circuit still can't be read this way, that's logged
  clearly rather than silently ignored - it likely means that row is a
  manually-entered schedule-only value with no backing `ElectricalSystem`
  element, which genuinely cannot be balanced/moved via the API since
  there's no real circuit object to move - a case worth checking for in
  the model directly if the warning appears.
- **"Imbalance before" reported a false 0% for a panel that clearly
  needed 26 circuit moves.** Just like `circuit.slot`, `circuit.phases`
  was never populated from real Revit data either - `_build_circuit_info`
  never set it, so every circuit started with an empty tuple, and
  `compute_phase_loads` sums nothing for a circuit with no phases,
  making every phase's "before" total look like 0 regardless of the
  panel's actual starting state. Fixed: every circuit with a known slot
  (from the TWELFTH/FOURTEENTH corrections above) now gets its phases
  derived from that slot via `arrangement_service.phase_for_slot` (made
  public for this reuse), spanning `poles` consecutive slots for
  multi-pole circuits - so "before" now reflects the panel's real
  starting phase distribution, not a false "already balanced" reading.
- **The multi-pass resolver kept repeating identical "cannot move"
  warnings with no progress.** Its occupied-slot bookkeeping tracked only
  a single slot per circuit regardless of pole count, so a multi-pole
  circuit's non-primary slots were invisible to it - the resolver kept
  believing a destination was free (per its incomplete bookkeeping) when
  Revit correctly rejected it as occupied. Fixed by changing
  `resolve_moves_with_staging`'s move tuples to `(current_slot,
  target_slot, poles)` and tracking full slot RANGES (both for occupancy
  checks and for finding a long-enough free run to stage through), with a
  new regression test (`test_resolver_respects_multi_pole_slot_ranges`)
  exercising exactly this scenario.
- **3-pole circuits weren't maintaining R-Y-B sequence during
  balancing - and it turned out to be structurally impossible under the
  prior code, not just an edge case.** A regression test written to
  confirm correctness instead proved the opposite: even a single 3-pole
  circuit in an otherwise completely empty 42-slot panel could not be
  phase-aligned. Root cause: `phase_for_slot`'s own convention pairs 2
  slots per phase (slots 1,2->R; 3,4->Y; 5,6->B; ...), matching real
  printed schedules where CKT R1 and R2 sit on the same row and share a
  phase - but the multi-pole search was looking for 3 *consecutive
  integers* to equal exactly (R, Y, B), which can never happen under a
  2-per-phase pairing (any 3-slot window spans at most 2 distinct
  phases). A 3-pole breaker physically spans 3 different ROWS (one per
  phase), which under this numbering means slots 2 apart in the SAME
  column (e.g. 1, 3, 5 - left column, rows 1-3 - reads R, Y, B) - not 3
  raw consecutive slot numbers. Fixed by stepping by 2 throughout the
  search (`_find_consecutive_run` / `_find_any_consecutive_run`), and by
  introducing `circuit_slot_range(start_slot, poles)` as the single
  source of truth for "which slots does this circuit occupy" - used
  consistently by the phase-derivation fix above, the occupancy
  bookkeeping in `_apply_slot_plan`, and the move resolver, so these
  three places (which had each separately assumed step-of-1 at
  different points) can never drift apart again. The move resolver
  itself was also refactored to accept explicit slot LISTS
  (`{key: (current_slots, target_slots)}`) rather than a "primary slot
  + poles count + assumed stepping" tuple, removing the last place a
  stepping convention could silently diverge from `circuit_slot_range`.
  New tests (`test_three_pole_circuit_misalignment_produces_a_warning`,
  `test_circuit_slot_range_three_pole_steps_by_two`,
  `test_resolver_respects_multi_pole_slot_ranges`) pin all of this down.
  **Still needs live verification:** this fix corrects the internal
  Python logic to be self-consistent (and matches what printed
  schedules actually show), but whether Revit's own `MoveSlotTo` truly
  relocates a 3-pole circuit's poles using this same step-of-2 slot
  numbering internally has not been confirmed against a live Revit
  session - if a 3-pole circuit still doesn't land correctly after this
  fix, that would point at a mismatch between our slot-numbering
  convention and Revit's own internal one, not this alignment logic.
- **A prior revision's comment claimed misaligned multi-pole placements
  would be "flagged via description note" - no such flag was ever
  actually implemented.** `build_slot_plan` now genuinely returns
  `(plan, warnings)`, with a specific, actionable warning identifying
  exactly which circuit landed on which slots and what phase sequence it
  actually got, surfaced through both Preview and Execute (live progress
  messages, not just the log file) instead of silently sacrificing
  alignment with no visible trace.
- **Reports showed VA where Watts were expected.** VA (Apparent Power)
  and Watts (Real/True Power) are genuinely different physical quantities
  related by power factor (W = VA x PF) - Revit's branch-circuit
  "Apparent Load" parameter (what this tool read) is natively VA, which
  is also the electrically correct basis for the balancing math itself
  (phase balancing equalizes current draw, which tracks VA, not real
  power) - so that math is unchanged. Reporting now computes a
  Watts-based figure (`CircuitInfo.true_load_w`) as VA x Power Factor
  when a Power Factor parameter is readable on the circuit, shown as the
  PRIMARY figures in the results grid (matching how a project's own
  printed schedule labels its totals), with VA kept as a secondary
  column for transparency about the balancing basis. This is an
  approximation, not a replication of Revit's own project-wide
  category/demand-factor Watts calculations (the "CONN./DEMAND WATTS"
  columns on a printed schedule), which depend on Electrical Settings
  this tool doesn't have access to.
- **3-pole circuits reportedly still not reading R-Y-B after the
  EIGHTEENTH correction's planning fix.** Rather than guess at another
  possible cause, a definitive post-move verification step was added:
  right after each panel's transaction commits, the schedule's ACTUAL
  final circuit positions are re-scanned (a safe, read-only call) and
  checked against a genuine R-Y-B sequence for every 3-pole circuit. If
  a mismatch is found, this is now logged as a specific, loud
  "VERIFICATION FAILED" warning identifying the exact circuit and its
  actual landing phases - distinguishing, with certainty, whether a
  reported misalignment is a planning issue (already caught separately
  by `build_slot_plan`'s own warnings) or a genuine mismatch between this
  tool's step-of-2 slot-numbering convention and how Revit's own
  `MoveSlotTo` actually relocates a multi-pole circuit's poles. This
  verification is what will finally confirm or rule out the "still needs
  live verification" caveat from the EIGHTEENTH correction. Related:
  spare/space not landing at the "bottom" of the schedule is most likely
  the same underlying symptom (circuits not landing where planned
  leaves capacity fragmented in ways that push spare/space fill into
  whatever gaps remain, rather than a clean trailing block) - the
  verification warning is the fastest way to confirm this connection on
  the next run.
- **CRITICAL, present since this tool's first version: every load figure
  was ~10.76x too large, because Revit's raw parameter value was never
  converted to real VA.** A user reported "Total Connected Load" not
  matching Revit's own printed schedule (449716.2 vs. the schedule's own
  41780) - the ratio, 10.7639..., matches EXACTLY `1 / 0.3048^2` to 6
  significant figures, and is independently confirmed by multiple
  Autodesk API forum threads describing `BuiltInParameter
  .RBS_ELEC_APPARENT_LOAD` returning a raw internal-units value that must
  be multiplied by `0.3048^2` to get real-world VA (Revit represents this
  quantity internally using a square-feet-based placeholder unit basis).
  This conversion was missing from every load value this tool has ever
  read, from its very first version. **Important nuance:** this did NOT
  make the balancing/imbalance-percentage math incorrect - every
  circuit's load was inflated by the exact same constant factor, so
  relative comparisons (which phase is heavier, by what percentage)
  stayed valid throughout; only the ABSOLUTE "Total Load" and per-phase
  VA/W figures shown in reports were wrong. Fixed with
  `_va_from_internal_units`, applied at the single point `_build_circuit
  _info` reads `RBS_ELEC_APPARENT_LOAD`. **Related, unconfirmed risk
  worth checking:** breaker rating (Amps) writes for spares use the same
  category of Revit internal-unit representation for several electrical
  parameter types - there's no direct evidence this is also affected, but
  given how this exact bug was found, it's worth specifically confirming
  a spare's rating (e.g. "20A") displays correctly on the printed
  schedule rather than assuming it's fine.
- **Spare/space still not landing at the bottom - restructured to reserve
  the trailing block BEFORE arrangement, instead of hoping leftover gaps
  end up there.** A user correctly identified the underlying design flaw:
  filling spare/space AFTER arrangement means their position depends
  entirely on wherever real circuits happen to leave gaps, which isn't
  reliable. In WaysTable mode, `execute_batch` now determines spare/space
  needs and finalizes the panel's right-sized capacity (via the ELEVENTH
  correction's grow/shrink logic) BEFORE arrangement runs, then reserves
  that exact trailing block of slots (`arrangement_service.build_slot_plan`
  gained a `reserved_slots` parameter, treated exactly like a locked
  circuit's slot - removed from the free pool up front, so no real
  circuit can ever be planned into it). Arrangement then only works with
  the remaining slots, and `fill_spare_and_space` afterward naturally
  finds ONLY the reserved block still empty (since nothing else could
  have used it) - no changes needed to its own placement logic. A new
  test (`test_reserved_slots_are_excluded_from_arrangement`) confirms the
  reservation holds.
- **The R-Y-B verification result was only ever logged, easy to miss, and
  turned out to be exactly the piece of information this tool's users
  kept being asked for but couldn't easily confirm either way.** The
  post-move verification (TWENTY-FIRST correction) is now surfaced
  directly in the Step 5 results grid as a **"3-Pole R-Y-B"** column -
  "OK" if every 3-pole circuit landed on a genuine R-Y-B sequence, or the
  specific circuit name(s) and actual phase order otherwise - plus a
  loud warning in the results summary banner if anything is misaligned.
  Confirming this no longer requires digging through pyRevit's log
  panel at all.
- **Multi-pole 2-pole phase pairing** on 3-phase panels assumes standard
  adjacent-leg pairing (e.g. R‑Y, Y‑B, B‑R); confirm this matches your
  panel manufacturer's bus configuration before relying on the arrangement
  output for construction documents.
- **Apparent load source**: `RBS_ELEC_APPARENT_LOAD` is read directly from
  each `ElectricalSystem`; projects using custom electrical calculation
  parameters (demand factors, connected vs. demand load toggles) should
  confirm this maps to "connected load" as intended by requirement 3.

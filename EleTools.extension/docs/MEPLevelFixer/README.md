# MEP Level Fixer

A pyRevit extension that re-associates MEP elements with the project level that
matches their **real physical elevation**, and recalculates the offset so the
element stays exactly where it is.

Typical cause of the problem: copy/monitor, linked model updates, design
changes or plain user error leave an electrical fixture that is physically on
Level 01 reporting `Reference Level = Level 00` with a compensating offset.
The model looks right; schedules, view filters, IFC export, level-based
quantities and coordination reports are all wrong.

---

## 1. Installation

### Prerequisites

| Item | Requirement |
|---|---|
| Revit | 2022, 2023, 2024, 2025 or 2026 |
| pyRevit | 4.8+ (Revit 2022-2024) or 5.x (Revit 2025-2026) |
| Engine | IronPython (the pyRevit default) |

### Option A - user install (single machine)

1. Copy the whole `MEPLevelFixer.extension` folder to your pyRevit extensions
   directory, typically:
   `%APPDATA%\pyRevit\Extensions\MEPLevelFixer.extension`
2. In Revit: **pyRevit → Reload** (or restart Revit).
3. A new **MEP Tools** tab appears with a **Level Tools** panel and the
   **Fix MEP Levels** button.

### Option B - CLI install

```bat
pyrevit extend ui MEPLevelFixer "<path-or-git-url>" --dest="%APPDATA%\pyRevit\Extensions"
pyrevit reload
```

### Option C - company deployment (recommended)

1. Put the extension on a network share or in a Git repository, e.g.
   `\\server\bim\pyrevit\extensions\MEPLevelFixer.extension`.
2. Register the folder once per machine (scriptable, no admin rights needed):

   ```bat
   pyrevit extensions paths add "\\server\bim\pyrevit\extensions"
   pyrevit reload
   ```

3. For a Git-hosted copy, `pyrevit extensions update --all` in a login script
   keeps every workstation on the same version.

**Verification:** click the button, open the category window, press *Cancel*.
If the window renders and `%APPDATA%\pyRevit\MEPLevelFixer\logs\` contains a
new log file, the install is healthy.

---

## 2. Folder structure

```
MEPLevelFixer.extension/
├── extension.json                      Extension metadata
├── README.md                           This file
├── docs/
│   └── sample_report.csv               Example of the generated report
├── lib/
│   └── mepleveler/                     Importable library (pyRevit adds lib/ to sys.path)
│       ├── __init__.py
│       ├── revit_compat.py             Revit 2022-2026 API shims
│       ├── log.py                      Logging framework
│       ├── config.py                   Category catalogue + persisted settings
│       ├── geometry.py                 Elevation extraction + movement fingerprints
│       ├── levels.py                   Level cache + matching strategies
│       ├── params.py                   Level/offset parameter discovery
│       ├── engine.py                   Collection, analysis, transactional execution
│       ├── reporting.py                CSV/Excel report + output-window summary
│       ├── main.py                     Controller (the tool workflow)
│       └── ui/
│           ├── MainWindow.xaml         Scope, options and category picker
│           ├── main_window.py
│           ├── PreviewWindow.xaml      Preview grid
│           ├── preview_window.py
│           └── progress.py             Progress window
└── MEP Tools.tab/
    └── Level Tools.panel/
        └── Fix MEP Levels.pushbutton/
            ├── bundle.yaml
            ├── icon.png
            └── script.py
```

---

## 3. Workflow

1. **Launch** – *MEP Tools → Level Tools → Fix MEP Levels*.
2. **Main window**
   - *Processing scope*: selected elements / current view / entire model.
   - *Level matching*: strategy, elevation source, tolerances.
   - *Categories*: multi-select checklist with search, Select all / Select none,
     live element counts, grouped by discipline. Selections are remembered
     between sessions.
   - *Target levels*: checklist of every level in the model, highest first,
     with its elevation. Only ticked levels can ever be assigned to an element.
     The selection is remembered **per model**.
   - Press **Preview…** to review first, or **Execute** to run after a single
     confirmation.
3. **Analysis** – a read-only pass. No transaction is opened, so nothing can be
   modified at this stage. Cancellable from the progress window.
4. **Preview window** – element id, category, family, type, current level,
   proposed level, current offset, proposed offset, status and message.
   Filter by category and status, free-text search, sort by any column,
   untick individual rows, zoom to or select elements in Revit, export the
   preview to CSV.
5. **Execution** – one `TransactionGroup` (assimilated into a single undo step)
   containing batched transactions. Each element is applied in its own
   `SubTransaction` and verified.
6. **Report** – a CSV report is written to
   `%APPDATA%\pyRevit\MEPLevelFixer\reports\` and a summary is printed to the
   pyRevit output window with clickable element links.

### Processing modes at a glance

| Scope | Collector | Notes |
|---|---|---|
| Selected elements only | `FilteredElementCollector(doc, selectedIds)` | Fastest; ideal for spot fixes |
| Current view only | `FilteredElementCollector(doc, view.Id)` | Respects view filters and crop |
| Entire model | `FilteredElementCollector(doc)` | Use with a category subset on large models |

---

## 4. How "nothing moves" is guaranteed

Changing a level parameter behaves differently per element type: some elements
keep their offset value (and therefore jump), others have their offset
recomputed by Revit. Rather than guessing, the engine **measures**:

1. Fingerprint the element (location point, curve endpoints or bounding box,
   plus facing/hand orientation for family instances).
2. Optionally fingerprint every connected neighbour via the `ConnectorManager`.
3. Open a `SubTransaction`, write the new level id, `doc.Regenerate()`.
4. Measure the displacement.
   - Any **horizontal** movement → roll back, report as failed.
   - Any **vertical** movement `dz` → add exactly `dz` to the offset
     parameter(s) and regenerate again. Sloped pipes and ducts get both
     *Start Offset* and *End Offset* shifted by the same amount.
5. Re-measure. Residual displacement above the tolerance (default 0.0005 ft
   ≈ 0.15 mm), a changed orientation, or a neighbour that moved → roll back
   and report as failed.
6. Only then is the `SubTransaction` committed.

The result: the element is corrected **or** it is untouched. There is no third
outcome, and a failed element never leaves the model in a half-edited state.

### Level and offset parameters used

| Element | Level parameter | Offset parameter |
|---|---|---|
| Duct / Pipe / Conduit / Cable Tray | `RBS_START_LEVEL_PARAM` (*Reference Level*) | `RBS_OFFSET_PARAM` (*Offset* / *Middle Elevation*) |
| Sloped pipe / duct | `RBS_START_LEVEL_PARAM` | `RBS_START_OFFSET_PARAM` + `RBS_END_OFFSET_PARAM` |
| Fittings / accessories | `RBS_START_LEVEL_PARAM` | `RBS_OFFSET_PARAM` |
| Level-based family instance | `FAMILY_LEVEL_PARAM` (*Level*) | `INSTANCE_ELEVATION_PARAM` (*Elevation from Level*) |
| Work-plane based instance | `SCHEDULE_LEVEL_PARAM` | `INSTANCE_FREE_HOST_OFFSET_PARAM` |
| Face-hosted device | `INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM` (*Schedule Level*) | none needed |

If none of the built-in parameters apply, a name-based search picks up shared
parameters used by manufacturer content.

### Hosted elements

Face-hosted and work-plane-based families (sockets on walls, detectors on
ceilings, devices on equipment) usually expose a **read-only** *Level*
parameter. Writing it is impossible, and forcing a host change would break the
hosting relationship. For these the tool writes **Schedule Level** instead:
schedules, view filters, level-based quantities and IFC export all become
correct, while the host, the work plane and the geometry are never touched.
This behaviour is controlled by the *Use "Schedule Level" for face hosted
elements* option.

### MEP systems

Duct, pipe, electrical, conduit and cable tray connectivity is preserved
because connectors are never disconnected and the element's final position is
identical. With *Verify connected elements* enabled, every neighbour reachable
through the element's connectors is fingerprinted and re-checked, so even a
transient drag is caught and rolled back.

---

## 5. Level matching strategies

| Strategy | Behaviour | Use when |
|---|---|---|
| **Nearest level** (default) | Smallest absolute vertical distance | General purpose; matches "which level is this thing closest to" |
| **Level at or below** | Highest level whose elevation ≤ element elevation | Storey logic: everything between Level 01 and Level 02 belongs to Level 01 |
| **Level below, snapping up** | As above, but an element up to *snap tolerance* below a level snaps up to it | Floor-mounted equipment modelled a few mm below the slab level |

**Elevation source** decides which point of the element is tested: insertion
point, midpoint, bounding-box centre, or *Automatic*, which applies a
category-specific rule (linear runs → midpoint, devices → insertion point,
equipment → bounding-box centre).

### Target levels

Most models carry levels that must never appear as a Reference Level: T.O.S.
and T.O.C. levels, ceiling references, parapet and datum planes, levels copied
in from architectural or structural links, and old levels left behind by design
changes. The **Target levels** checklist decides which levels the tool is
allowed to allocate to.

- Levels are listed top to bottom with their elevation in project units.
- Only ticked levels are candidates. An element sitting on an unticked level is
  reallocated to the nearest ticked one, and the report message says
  `Current level "<name>" is not a target level.`
- Unticked levels are still resolved for display, so the *Current level* column
  always shows the real name.
- **Filter box** – type e.g. `T.O.S` to isolate matching levels, then press
  *Untick shown*. *Tick shown* does the opposite. *Invert* flips everything.
- **Levels in use** – counts how many elements in the current scope reference
  each level and ticks only those, which is a quick way to see what the model
  actually uses before curating the list.
- The selection is stored per model (keyed on the document title) in
  `settings.json`, so each project keeps its own storey list and a local copy,
  a detached copy and the central file share one entry.
- Execution is blocked if no level is ticked.

The report header records both `Target levels` and `Levels ignored` so a
reviewer can see exactly which list was in force.

---

## 6. Error handling

Every condition below is detected, reported per element, and never aborts the
run:

| Condition | Handling |
|---|---|
| Read-only level parameter | Falls back to *Schedule Level*, else reported as *Unsupported* |
| No level parameter at all | Reported as *Unsupported* with the reason |
| Read-only offset parameter | Start/End offset pair tried, else rolled back and reported |
| Invalid or missing geometry | Reported as *Error*: "no usable geometry or location" |
| Grouped elements | Skipped by default (option) |
| Design option elements | Optional skip |
| Pinned elements | Optional skip |
| Nested components | Skipped by default: they follow their parent |
| Linked model elements | Never collected; linked docs are rejected up front |
| Workshared ownership conflict | Elements are checked out first; elements owned by another user are skipped with the owner's name |
| Revit errors during the batch | `IFailuresPreprocessor` swallows warnings, rolls back on errors; the affected elements are reported |
| User cancels mid-run | The whole transaction group is rolled back |

---

## 7. Performance

Tested against models with 10,000+ MEP elements:

- one `FilteredElementCollector` pass with an `ElementMulticategoryFilter` for
  both the counts and the collection;
- analysis is fully read-only, so Revit never regenerates during it;
- execution is batched (default 250 elements per transaction) and assimilated
  into a single undo step;
- the progress window repaints every 10 elements rather than every element,
  which is the single biggest saving on large runs;
- `doc.Regenerate()` is called only for elements that actually change.

Tune `batch_size` in `settings.json` if your hardware prefers larger or smaller
batches.

---

## 8. Settings, logs and reports

All under `%APPDATA%\pyRevit\MEPLevelFixer\`:

- `settings.json` – last used scope, categories, strategy, options and the
  per-model target level selection (`level_selection_by_model`).
- `logs\mepleveler_<timestamp>.log` – one file per run, last 25 kept.
- `reports\MEPLevelFixer_<model>_<timestamp>.csv` – UTF-8 with BOM, quoted
  fields, opens directly in Excel. See `docs/sample_report.csv`.

The report contains a header block (model, scope, strategy, tolerance,
categories, levels considered), a summary block (scanned / updated / skipped /
failed) and one row per element with old and new level, old and new offset in
both project units and decimal feet, the parameters used, the residual
movement, the status and any error message.

---

## 9. Recommended practice

1. Run on a **detached local copy** first and review the report.
2. Start with *Current view only* and one discipline to validate the strategy
   against your level naming before running model-wide.
3. Use **Preview** for the first run of any new project; once the settings are
   proven, **Execute** is safe for routine cleanups.
4. Synchronise before and after a model-wide run on a workshared project.
5. The whole run is a single undo step: `Ctrl+Z` reverts everything.

---

## 10. Extending the tool

- **New category**: add one row to `CATEGORY_DEFS` in `config.py`.
- **New elevation rule**: add an entry to `CATEGORY_ELEVATION_RULES`.
- **New parameter convention**: append to the candidate lists in `params.py`.
- **New matching strategy**: add a `_resolve_*` method to `LevelResolver` and
  register its label in `config.STRATEGY_LABELS`.
- **Level allow-list**: handled by `LevelResolver.allowed_names`; the keyword
  based `excluded_level_keywords` setting still works as a secondary filter and
  can be edited directly in `settings.json` for a standing company rule.

No other file needs to change for any of these.

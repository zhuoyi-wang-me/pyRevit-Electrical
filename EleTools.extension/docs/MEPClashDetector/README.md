# MEP Clash Detector — pyRevit Extension v1.0

Hard-clash detection of **Cable Trays, Ducts, Pipes and their fittings** in the **active floor plan view** — against each other and against user-selected **Architecture and Structure** categories — in the **host model and in Revit links**, with highlighting, clash sets, a modeless results window, XLSX export and Revit selection sets.

---

## 1. Folder structure

```
MEPClashDetector.extension/
├── extension.yaml                         # extension metadata (optional)
├── README.md
└── MEP.tab/
    ├── bundle.yaml                        # ribbon tab title
    └── Coordination.panel/
        ├── bundle.yaml                    # ribbon panel title
        └── ClashDetector.pushbutton/
            ├── script.py                  # all logic (collection → UI → export)
            ├── ui.xaml                    # WPF results window (modeless)
            ├── scope.xaml                 # Clash Scope dialog (models + ARC/STR)
            ├── icon.png                   # 96×96 ribbon icon
            └── bundle.yaml                # button title, tooltip, version range
```

pyRevit builds the ribbon from the folder names: `*.extension` → `*.tab` → `*.panel` → `*.pushbutton`. Renaming a folder renames the UI element.

---

## 2. Installation

### 2.1 Prerequisites

| Item | Requirement |
|---|---|
| Revit | 2023, 2024, 2025 or 2026 |
| pyRevit | 4.8.x (Revit 2023–2024) or 5.x (Revit 2025–2026) |
| Engine | Default IronPython engine (required; CPython is not supported by the WPF window). |
| openpyxl | Optional. Only used by the CPython export path — a built-in .NET writer covers IronPython. |

### 2.2 Single user (fastest)

1. Copy `MEPClashDetector.extension` to a permanent folder, e.g.
   `C:\pyRevitExtensions\MEPClashDetector.extension`
   (**not** inside the pyRevit install folder — it would be wiped on update).
2. Revit → **pyRevit** tab → **Settings** → *Custom Extension Directories* → **Add Folder** → select `C:\pyRevitExtensions`.
3. **Save Settings and Reload**. The **MEP** tab appears with a **Coordination** panel.

### 2.3 Command line (scriptable)

```bat
pyrevit extend path "C:\pyRevitExtensions"
pyrevit reload
```

### 2.4 Git-based deployment (recommended for teams)

```bat
:: one-off on each workstation
pyrevit extend ui MEPClashDetector https://github.com/<org>/MEPClashDetector.extension.git
pyrevit reload

:: later updates
pyrevit extensions update MEPClashDetector
```

Every user then gets new versions by pulling, with no manual file copying.

### 2.5 Enterprise / locked-down deployment

* Put the extension on a read-only network share (`\\server\BIM\pyRevit\Extensions`).
* Push the custom extension path with a machine-level `pyRevit_config.ini` or via `pyrevit configs` in your Intune/SCCM package.
* Add `rocket_mode_compatible: true` (already set) so pyRevit's fast-load mode stays enabled.

---

## 3. Usage

1. Open a **floor plan** view and set the view range / crop / visibility so that only the services you want to check are visible — this is also the main performance lever.
2. **MEP → Coordination → Clash Detector**.
3. **Clash Scope dialog** (remembered per model):
   * **Models** – one row for the host and one per Revit link. Tick **MEP** to use the model's cable trays / ducts / pipes, tick **Arch / Struct** to use its architecture / structure elements. Links are pre-ticked from their file name (`…ARC…`, `…STR…`, `…MECH…`, `…ELEC…`); unloaded links are greyed out.
   * **Architecture categories** – walls, floors, ceilings, roofs, columns, stairs, doors, windows, railings, ramps, curtain panels / mullions, generic models.
   * **Structure categories** – structural columns, framing (beams), foundations, structural walls, structural floors / slabs. Walls and floors are split by their *Structural* checkbox.
   * **Also check MEP vs MEP** – the cable tray / duct / pipe matrix across all ticked models (host vs link and link vs link included).
   * Rules: MEP vs ARC/STR and (optionally) MEP vs MEP. ARC/STR vs ARC/STR is never checked.
4. Wait for the progress bar (cancellable).
5. The **modeless** results window opens — you can keep working in Revit (move a duct, re-route a pipe) with the window open, then press **Refresh**.
   * **Left pane – Clash Sets.** Clashes whose clash points are within the *Set radius* (default 1000 mm, 3D) of each other form one set; `Set-001` is the biggest hotspot. Sets with several clashes are shown in bold. Change the radius and press **Regroup** (or Enter) — no model change. Tick **Multi-clash sets only** to hide single clashes.
   * **Right pane – Clashes** of the selected set (**Show all** lists every clash).
   * **Click** a set or clash → selects its elements and zooms to the clash location. **Double-click** → isolates it.
   * **Search box / Clash type / Class** (MEP vs MEP / ARC / STR) filter both panes. **Column headers** sort. Every clash shows its **Source** (Host or link name) and discipline.
   * **Change Scope…** reopens the Clash Scope dialog and re-runs.
   * **Export to Excel** → `Clashes` (with Set column), `Clash Sets` and `Summary` sheets.
   * **Save Selection Sets** → one Revit Selection Filter per set, named `Clash Set - <view> - Set-###` (Manage → Selection → Load, or use them in view filters). Re-saving updates them and removes leftovers.
   * **Refresh / Isolate / Clear Highlights**.
6. **Linked elements**: Revit cannot colour-override individual linked elements, so only host elements are coloured (host Arch/Struct elements in **orange**). Clicking a clash **selects the linked element itself** (link reference) and zooms to the clash point; *Isolate* keeps the whole link instance visible. Selection sets can only store host elements.
7. Linked elements are collected inside the view **crop region** (if active) and **view range** (view depth → top clip), independent of the link's visibility in the view — the Clash Scope dialog decides which links are used.
8. Closing the window **keeps** the colour overrides. Running the tool again resets colours on elements that no longer clash; **Clear Highlights** removes them all.
9. Clicking the ribbon button again while the window is open brings it to the front (or replaces it if you are in another view). The window closes itself when its model is closed. If you switch to another model, actions ask you to switch back.
10. **Close the results window before pyRevit → Reload.**

### Colour key

| Colour | Applied to |
|---|---|
| Red | Cable Trays |
| Blue | Ducts |
| Green | Pipes |
| Magenta | All fittings (cable tray, duct, pipe) |
| Orange | Host Architecture / Structure elements |
| — | Linked elements are not coloured (Revit limitation) — they are selected on click |

---

## 4. Configuration

All tuning lives in the `Config` class at the top of `script.py`:

| Setting | Default | Effect |
|---|---|---|
| `MIN_CLASH_VOLUME` | `0.0001` ft³ | Intersection volume threshold. Raise it to suppress "touching" clashes. |
| `BBOX_TOLERANCE` | `0.0` ft | Grows/shrinks bounding boxes before the broad-phase test. Set to `0.083` (1") to report near-misses. |
| `GRID_CELL_SIZE` | `10.0` ft | Spatial-hash cell size. Smaller = more cells, fewer candidate pairs. |
| `DETAIL_LEVEL` | `Fine` | `Medium` is ~30–40 % faster but fittings become coarser. |
| `IGNORE_SAME_SYSTEM` | `True` | Skips pairs with an identical, non-empty MEP system name. |
| `INCLUDE_FITTING_TO_FITTING` | `False` | Adds fitting↔fitting combinations (outside the v1 spec). |
| `LARGE_MODEL_WARNING` | `6000` | Elements above which the user is asked to confirm. |
| `OVERRIDE_TRANSPARENCY` / `OVERRIDE_LINE_WEIGHT` | `0` / `6` | Appearance of the highlight. |
| `SET_RADIUS_MM` | `1000` | Default clash-set radius (single-linkage, 3D). `0` = no grouping. Editable in the window. |
| `SELECTION_SET_PREFIX` | `Clash Set` | Name prefix of the saved Revit Selection Filters. |
| `ZOOM_PADDING_FT` | `3.0` | Margin around the clash point(s) when zooming. |
| `LINK_REGION_MARGIN_FT` | `1.0` | Margin added to the view crop / view range when collecting linked elements. |
| `BUILDING_CATEGORIES` | list | ARC / STR categories offered in the dialog and their default ticks. |

---

## 5. How it works

| Stage | Implementation |
|---|---|
| **Collect (host)** | `FilteredElementCollector(doc, view.Id)` + `ElementMulticategoryFilter` — Revit itself excludes hidden elements, hidden categories, view filters and anything outside the crop. |
| **Collect (links)** | `FilteredElementCollector(linkDoc)` + `ElementMulticategoryFilter` + `BoundingBoxIntersectsFilter` with an `Outline` built from the view crop box and view range, inverse-transformed into link coordinates. Bounding boxes and solids are moved into host coordinates with `RevitLinkInstance.GetTotalTransform()` / `SolidUtils.CreateTransformed`. Stacked-wall parents are skipped (their sub-walls carry the geometry). |
| **Rules** | MEP vs MEP via the category matrix (optional); MEP vs ARC/STR always; ARC/STR pairs never. Same-system suppression only applies inside one model. The MEP element is always reported as side A. |
| **Linked selection** | `Reference(element).CreateLinkReference(linkInstance)` + `Selection.SetReferences` (Revit 2023+); falls back to selecting the link instance. |
| **Broad phase** | Uniform 3D spatial hash (`GRID_CELL_SIZE`) → only elements sharing a cell are paired, then filtered by allowed category combination, AABB overlap and system name. Complexity drops from O(n²) to roughly O(n). |
| **Narrow phase** | `BooleanOperationsUtils.ExecuteBooleanOperation(solidA, solidB, Intersect)`; a clash is reported when the resulting solid's `Volume > MIN_CLASH_VOLUME`. The clash point is `Solid.ComputeCentroid()`. |
| **Geometry** | Extracted **lazily** and cached per element, recursing through `GeometryInstance.GetInstanceGeometry()` (already in model coordinates). Released after the run. |
| **Highlight** | `OverrideGraphicSettings` (projection + cut lines, solid surface/cut pattern) applied inside one transaction, with one `OverrideGraphicSettings` object cached per colour. |
| **Clash sets** | Union-find over the clash points with a uniform grid (cell = radius, 27-cell neighbourhood) → ~O(n). Z is included, so levels never merge. |
| **UI** | `pyrevit.forms.WPFWindow` loading `ui.xaml`, **modeless** (`Show()`). The window never calls the Revit API from its own handlers: each action is queued on a `RevitActionHandler` (`IExternalEventHandler`) and run via `ExternalEvent.Raise()` in a valid API context. Actions with the same key coalesce (fast arrow-key scrolling zooms once). `__persistentengine__` / `engine: persistent: true` keep the handlers alive after the command returns. |
| **Sorting/filtering** | Done in Python and re-bound to the grid, instead of relying on WPF's reflection-based `CollectionView` — this keeps behaviour identical on IronPython 2.7, IronPython 3 and CPython. |
| **Export** | `openpyxl` when importable (CPython engine): "Clashes" sheet + "Summary" sheet, formatted headers, freeze panes, autofilter, auto-fit columns. Otherwise a built-in `System.IO.Compression` SpreadsheetML writer produces a valid `.xlsx` with the same data plus a summary block — so the button works on stock IronPython with zero dependencies. |

---

## 6. Revit API references

```
Autodesk.Revit.DB      FilteredElementCollector, ElementMulticategoryFilter,
                       BuiltInCategory, BuiltInParameter, Options,
                       GeometryElement, GeometryInstance, Solid,
                       BooleanOperationsUtils, BooleanOperationsType,
                       OverrideGraphicSettings, FillPatternElement,
                       FillPatternTarget, Color, Transaction,
                       TemporaryViewMode, ViewPlan, ViewDetailLevel,
                       MEPCurve, ElementId, XYZ
Autodesk.Revit.UI      UIDocument.ShowElements, UIDocument.Selection
pyrevit                forms (WPFWindow, ProgressBar, alert, save_file),
                       script (get_logger, get_output, exit)
.NET                   System.Collections.Generic.List,
                       System.Collections.ObjectModel.ObservableCollection,
                       System.ComponentModel.ListSortDirection,
                       System.IO.Compression.ZipArchive
```

`RevitAPI.dll` and `RevitAPIUI.dll` are loaded by pyRevit automatically — no manual `clr.AddReference` is required for them.

---

## 7. Known limitations (v1)

* Linked models are ignored.
* Only floor plan views (`ViewPlan`) are accepted.
* Cable trays have no MEP system in Revit, so the "same system" filter never suppresses tray clashes.
* Elements whose geometry Revit cannot boolean (rare tessellated fittings) are logged at debug level and skipped rather than reported.
* Overrides are view-specific and are not written to a shared parameter, so they disappear if a user applies a view template that disallows overrides.

---

## 8. Future enhancements

1. **Linked model support** — collect from each `RevitLinkInstance`, transform link solids with `GetTotalTransform()` before the boolean, and add a "Source model" column.
2. **Tolerance / clearance clash mode** — offset solids (`SolidUtils` or `Solid` scaling) to catch insulation and maintenance-clearance violations, plus a dedicated "soft clash" severity column.
3. **3D view + section box navigation** — create a temporary 3D view per clash and drive its section box to the clash point, like Navisworks' viewpoint navigation.
4. **Clash markup** — place a generic annotation family or a Revit "Clash" family instance at each clash point so clashes live in the model and can be scheduled.
5. **Status tracking** — persist clash state (New / Active / Reviewed / Approved / Resolved) in extensible storage or a JSON side-car keyed by element-ID pair, so repeat runs show what changed.
6. **BCF 2.1 export** — generate BCF issues with snapshots for Solibri/Navisworks/BIM Track round-tripping.
7. **Rule editor UI** — let coordinators enable/disable category pairs, per-pair tolerances and system exclusions in a settings window (`script.get_config()` for persistence).
8. **Multi-view / whole-model batch mode** — iterate a selected set of views or the entire model and merge the results, with `pyrevit run` CLI support for overnight runs.
9. **Parallel narrow phase** — the boolean stage is CPU-bound and independent per pair; a C# helper assembly with `Parallel.ForEach` (the Revit API geometry read is thread-safe for already-extracted solids) would cut run times significantly on large views.
10. **Reporting upgrades** — conditional formatting and a pivot/chart sheet in the XLSX, plus a paired PDF/HTML report generated from the pyRevit output window.
11. **Interference Check API cross-check** — compare results against `Document.Interference` / Revit's built-in Interference Report as a validation harness.
12. **Element filters** — exclude by workset, phase, size threshold (e.g. ignore pipes < 25 mm) or by a project-specific "Coordination Exempt" parameter.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Button greyed out / missing | Check the custom extension directory in pyRevit Settings and reload. Confirm folder names end in `.extension`, `.tab`, `.panel`, `.pushbutton`. |
| "This tool must be run from a FLOOR PLAN view" | Activate a floor plan (not a ceiling plan, 3D or section). |
| No clashes but you expect some | Elements may be hidden or outside the crop; the view controls the scope. Also check `MIN_CLASH_VOLUME` and whether both elements share a system name. |
| Detection is slow | Crop the view, hide categories, set `DETAIL_LEVEL = ViewDetailLevel.Medium`, or reduce `GRID_CELL_SIZE`. |
| Excel file looks plain | You are on the IronPython engine and the .NET fallback writer was used. Run on the CPython engine with `openpyxl` installed for full formatting. |
| Grid is empty but clashes were found | You are on an engine where WPF cannot bind to Python objects — switch the button to the default IronPython engine. |

Full tracebacks are written to the pyRevit output window; open it with **pyRevit → Show Output Window** or run the button with the log level set to DEBUG.

---

## Changelog

### 1.2.0
* **Linked models**: MEP, Architecture and Structure elements from Revit links (link transform applied to boxes and solids; collected within the view crop and view range).
* **Clash Scope dialog**: per model (host + each link) choose MEP and/or Arch/Struct use; choose ARC and STR categories (walls/floors split by the Structural flag); optional MEP vs MEP. Choices remembered per model; *Change Scope…* in the results window.
* New columns: Clash Class, Source A/B, Discipline A/B; sets show linked count, classes and sources. Class filter. Export sheets updated.
* Host Arch/Struct elements highlighted orange; linked elements selected via link references; selection sets store host elements only (linked-only sets are skipped and reported).

### 1.1.0
* Results window is now **modeless** (ExternalEvent based, persistent engine, single-instance, closes with its model, guards against model switches).
* **Clash sets**: clashes at the same location are grouped (configurable radius, Regroup, multi-clash filter), shown in a Sets pane, exported to a `Clash Sets` sheet and savable as Revit Selection Filters.
* Zoom now centres on the clash point(s) with `UIView.ZoomAndCenterRectangle` instead of the full element extents.
* Excel export: separate `Clashes`, `Clash Sets` and `Summary` sheets in both writers.

### 1.0.1 — debug pass
* Refresh: declining the large-view warning raised `SystemExit` inside a WPF event handler (not caught by `except Exception`) — now a `DetectionCancelled` exception, previous results are kept.
* Highlights from previous runs are remembered per view and reset when elements no longer clash (including zero-clash runs). *Clear Highlights* now also clears them.
* Transactions check `Commit()` status and never roll back an already-ended transaction.
* Revit 2026: `ElementId.Value` / `ElementId(Int64)` used on 2024+ (`IntegerValue` and the `Int32` constructor were removed in 2026). `BuiltInCategory.value__` (IronPython-only) replaced by an engine-safe helper.
* System *classification* is no longer treated as a system name (it hid real clashes).
* Only Floor Plans are accepted (`ALLOWED_VIEW_TYPES` in `script.py`), matching the spec.
* Window: events wired in code (none in XAML), no handler fires during construction, double-clicking a header/scrollbar no longer isolates the selected row, sort arrow persists, cancelled runs are flagged in the status bar, *Open folder* uses Explorer.

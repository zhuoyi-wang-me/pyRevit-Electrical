# Visibility Manager (pyRevit extension)

A pyRevit tool for copying/removing view filters and V/G (category)
overrides across views and view templates. Three-column interface:
**[1] mode + source items**, **[2] target views**, **[3] target
templates** — each with search, live counts, and type-filter chips.

## Install

1. Requires **pyRevit 4.8+**. This build targets the **IronPython
   engine** (pyRevit's default) — no extra Python packages required
   except optionally `openpyxl` for true XLSX export (falls back to CSV
   automatically if it's missing).
2. Copy this whole `VisManager.extension` folder to your pyRevit
   extensions directory, e.g.:
   `%APPDATA%\pyRevit\Extensions\VisManager.extension`
3. In Revit: `pyRevit` tab → `Reload` (or restart Revit).
4. A new **VisManager** tab appears with a **Visibility** panel and
   **Visibility Manager** button.

## Using it

1. Open the source view, then click the button.
2. **[1]** Pick **Filters**, **V/G Overrides**, or **Revit Links** mode,
   then check the items to act on.
   - V/G mode: each category shows a `(Hidden)` / `(Override)` tag
     reflecting its current state in the source view.
   - Revit Links mode: each link instance shows its current display
     setting — `By Host View`, `By Linked View`, or `Custom (view name)`
     — matching the "Revit Links" tab of the V/G Overrides dialog.
3. **[2]** Check target views. Narrow by type (3D / Floor Plans /
   Sections / Ceiling Plans / Elevations — each chip shows a live
   count) and/or search by name.
4. **[3]** Same, but for view templates.
5. **COPY** applies the selection to every checked target in [2]/[3]:
   - Filters: assignment + override graphics + enabled/visibility state.
   - V/G Overrides: category override graphics + hidden state.
   - Revit Links: visibility type (By Host View / By Linked View /
     Custom + which linked view) and whether worksets follow the host
     view.
   **REMOVE** removes the filter, resets the category override to
   blank/unhidden, or resets a link back to "By Host View".
6. **EXPORT (CSV)** / **XLSX** write the selected source items — with
   full override data, human-readable (`RGB(0,255,0)`, pattern names
   like `Gypsum-Plaster|46555`, `Default` for unset) — to a file.
   **IMPORT** reads such a file back in and applies it to whatever
   targets are currently checked in [2]/[3], matching rows by
   filter/category/link **name** in the current project.

## Export column schema

**Filters / V/G Overrides:** `Name`/`Category`, `Status`, then
`Projection_Line_Color`, `Projection_Line_Pattern`,
`Projection_Line_Weight`, `Surface_Fill_Foreground_Color`,
`Surface_Fill_Foreground_Pattern`, `Surface_Fill_Background_Color`,
`Surface_Fill_Background_Pattern`, `Cut_Line_Color`, `Cut_Line_Pattern`,
`Cut_Line_Weight`, `Cut_Fill_Foreground_Color`,
`Cut_Fill_Foreground_Pattern`, `Cut_Fill_Background_Color`,
`Cut_Fill_Background_Pattern`, `Transparency`, `Halftone`.

**Revit Links:** `Name`, `Status`, `VisibilityType`
(`ByHostView`/`ByLinkView`/`Custom`), `LinkedViewName` (only set when
`VisibilityType` is `Custom`). Requires Revit 2024+; `Custom` requires
Revit 2025+ to *apply* (Revit 2024 can read/report it but
`SetLinkOverrides` only gained Custom support in 2025).

## Known limitations

- **Cross-project import**: pattern/linetype names are resolved and
  re-matched by name+id within the *same* project. Importing a file
  exported from a different model will only reliably restore colors,
  weights, transparency, and halftone — a pattern name that doesn't
  exist in the target project falls back to "Default" rather than
  erroring.
- **Filter rule definitions** (the actual match criteria) aren't
  reconstructed from an import file — import only re-applies graphic
  overrides to a filter that must already exist (by name) in the
  target project. Within the same project this is a non-issue since
  the filter element already exists everywhere.
- **Revit Links mode** is enabled by feature detection
  (`View.SetLinkOverrides` present), not by version number. If a Revit
  version rejects a particular visibility type, the error is reported
  per link/target in the output window.
- **Linked views must match the target's view type.** "By Linked View"
  / "Custom" copied to a different view type is remapped to a
  same-named view of the right type inside the link, otherwise reported.
- **Importing a Custom link row** restores the type and linked view only;
  Custom sub-settings (phase, filters, detail level...) aren't in the
  file. COPY between views preserves them fully.
- **Template-controlled targets are skipped** (reported in the output
  window). Tick the template in column [3] to change those views.
- **Linked-model worksets** (Custom display > Worksets tab) have no
  separate API. They are copied only if Revit carries them inside the
  link's display-settings object; check one link in V/G after copying.
- The V/G Revit Links **Halftone / Underlay** columns and **Custom
  sub-tabs** have no public API: use COPY > "Full tab (all links)", which
  lets Revit transfer them. Export/import can't carry them.
- No PAT/LIN pattern-file auto-export bundled with the XLSX/CSV yet —
  extension points are in `filters_core.py` / `io_utils.py`.

## Changelog

### v1.5
- **Revit Links COPY now offers "Full tab (all links)"** - a Revit-native
  copy of the entire V/G Revit Links tab: Visibility, **Halftone,
  Underlay**, and Display Settings including **every Custom sub-tab**
  (Basics, Model / Annotation / Analytical / Import Categories, Filters,
  Worksets...). The public API only exposes the display type and linked
  view, so these can't be set directly; instead a temporary template that
  controls ONLY "V/G Overrides RVT Links" is created from the source,
  applied to each target (`ApplyViewTemplateParameters`), then deleted.
  Nothing else on the target changes.
  Limits: all links at once; views only (not templates); same view type
  as the source. Halftone/Underlay/Custom can't be read back by the API,
  so spot-check one link in V/G.
- "Selected links only" keeps the per-link API method.

### v1.4
- Fixed "Revit didn't keep: visibility" on Revit Links:
  - A link's hidden state is only reliable after regeneration. v1.3 read
    it back immediately, saw the old value, reported "failed" and rolled
    back a change that had actually worked. The document is now
    regenerated before the check (only when a hide/unhide was made).
  - The V/G Revit Links tab has a **type row** (the .rvt) and nested
    **instance rows**; each can be unticked. v1.3 only handled the
    instance, so links hidden at the type row stayed hidden. Both rows
    are now read, copied, reset and reported ("Hidden (type)").
- Failures now show the type-row and instance-row state before and after,
  plus CanBeHidden.
- Adds a note when a link is un-hidden but still can't display because
  its workset or the "RVT Links" category is hidden in the view.

### v1.3
- **Revit Links mode now covers the whole V/G "Revit Links" row**:
  - Visibility checkbox (hidden/shown link instance) - NEW
  - Halftone (and any Override Graphics by Element on the link) - NEW
  - Display Settings - now compared on *every* property of the link
    settings (type, linked view, and all Custom sub-settings exposed by
    your Revit version), not just the "By Host View" label. v1.2 reported
    "already identical" whenever the labels matched.
  Each link runs as two jobs: `[Display Settings]` (template-controllable)
  and `[Visibility/Halftone]` (per view only; view templates can't hold
  it, so they show as skipped with a note).
- **New Worksets mode** (host model): copy/reset/export/import per-view
  workset visibility - Shown / Hidden / Using Global Setting. Respects
  templates that control "V/G Overrides Worksets". Workshared models only.
- Export/import for links adds `Visible` and `Halftone` columns.

### v1.2
- COPY / REMOVE / IMPORT now **read back** every target and report
  `changed` vs `already identical` vs `failed`. v1.1 reported "succeeded"
  whenever Revit didn't throw, even if nothing changed (e.g. copying to
  a duplicate of the source view).
- Per-target results table in the pyRevit output window.
- Warns when a target or the active view is in Temporary View
  Properties mode (Revit shows a temporary template, hiding real changes).
- Flags copied filters that sit lower in the target's priority order
  than in the source (a higher filter can mask their overrides).
- Active view is refreshed after the transaction.

### v1.1
- Added ribbon icon (`icon.png`) plus a dark-theme variant (`icon.dark.png`).
- Revit 2026: replaced `ElementId.IntegerValue` / `ElementId(int)`
  (removed from the 2026 API) with version-safe helpers (`compat.py`).
- View-template aware: source settings come from the template that
  actually controls them; target views controlled by a template are
  skipped with a reason instead of "succeeding" with no visible effect;
  templates whose Include box is off are flagged.
- Links: "By Linked View" now carries its linked view (was dropped);
  Custom sub-settings preserved on copy; view-type mismatch remapped or
  reported; linked view name exported for both By Linked View and Custom.
- V/G: subcategories added; overrides and visibility applied
  independently; import now un-hides as well as hides.
- Filters: import restores Enabled + Visible; new `Visible` column.
- Overrides: Solid line pattern no longer lost; fill-pattern visibility
  and Detail Level exported/imported; patterns resolved by name on import.
- Per-item SubTransactions, warnings suppressed, rolled-back
  transactions reported.
- Targets start unchecked, keep checks across chip changes, and only
  items visible under the current search are acted on.
- Fixed invalid import file-dialog filter; XLSX numbers (`5.0`) import.

## File layout

```
VisManager.extension/
  VisManager.tab/
    Visibility.panel/
      Visibility Manager.pushbutton/
        bundle.yaml         - button title/tooltip
        icon.png            - ribbon icon (96x96)
        icon.dark.png       - icon for Revit's dark theme
        script.py           - window logic (event handlers)
        VisManagerUI.xaml   - WPF layout (3-column)
  lib/
    vismanager/
      view_utils.py         - view/template discovery, 5-family grouping, counts
      filters_core.py       - filter copy/remove + override (de)serialization
      vg_core.py             - category override copy/reset + status tags
      links_core.py           - Revit Link display settings + visibility/halftone
      worksets_core.py        - host-model workset visibility per view
      io_utils.py             - CSV/XLSX export-import
      compat.py               - Revit 2023-2026 ElementId + view-template helpers
```

# Export Manager – UI redesign + second debug pass (2026-10-05)

## New look (`ExportManager.xaml`)
- Header band with the model name and **Refresh from model**; numbered step strip
  (Selection → Format → File name → Output); footer with a live status line,
  Back / Next and an always-visible green **EXPORT** button.
- Selection step: filters, view filters and selection sets moved to a left
  side panel, so the sheet/view list gets the full height. Search box with icon,
  placeholder and clear button; export range as a 3-way switch; "Tick all shown"
  shows a dash when only some rows are ticked.
- Format step: three cards. PDF cards grey out when PDF is off, the DWG card when
  DWG is off, the zoom % box when "Fit to page" is chosen.
- File name step: search box over the parameter list, monospace preview.
- Output step: **Ready to export** summary (what, formats, file-name example, folder).
- All styling lives in `Window.Resources`; every control `Name` and the `dgItems`
  column headers the Python reads are unchanged. Icons are Segoe MDL2 Assets glyphs
  (ships with Windows 10/11), no image files.

## Bugs fixed
1. **Custom separators were lost on Sheets → Views → Sheets** (and after loading a
   format): only the parameter names were remembered per mode, so every separator
   went back to `-`. Separators are now stored per mode (`filename_builder.py`).
2. **Sheet-filter grid re-ran the whole filter on every click / arrow key** and
   rebuilt itself from inside its own `CurrentCellChanged`, which could drop the
   highlighted row ("Select a filter row to remove"). It now re-applies only when
   an *On* box really changes, and the *On* box is one click (`sheet_filter_ui.py`).
3. **Revit event handlers leaked every time the window was closed with the X** –
   Revit refuses `-=` outside an API context, the error was swallowed. The detach is
   now queued through the ExternalEvent when the direct attempt is refused (`ui.py`).
4. **DWG file names**: the name is now passed to Revit with its `.dwg` extension so a
   dot in a sheet number (`E-1.01`) can never be taken for an extension; a missing
   output file is reported instead of being counted as exported (`export_dwg.py`).
5. **Combined PDF** was reported as exported without checking the file; it is now
   detected and renamed to the requested name like separate PDFs (`export_pdf.py`).
6. **Output folder**: every failure said "Select an output folder", even when the
   folder could not be created; a relative path (`Exports`) was silently resolved
   against Revit's working directory. Both now give a specific message.
7. **"Select All" box went stale** after a search/filter change and could untick
   when the user meant to tick. It now mirrors the rows on screen.
8. **Selection-set overwrite check** was case-sensitive and compared the raw typed
   name: `my set` silently replaced `My Set`, `A/B` replaced `A-B`.
9. Typing an unfinished `"phrase` in Search blanked the list until the quote was closed.
10. `dwg_ole_binder.py`: duplicate `wait_for_command_idle` removed; `Queue` import
    works on both IronPython engines.

## Small additions
- Validation errors jump to the step that needs fixing.
- Back / Next disable at the ends.
- The output folder is remembered per model
  (`%APPDATA%\EleTools\ExportManager\settings.json`).
- The window never opens larger than the screen.

---

# Export Manager – debug + modeless + view search

## New
- **Modeless window** (`script.py`, `ui.py`, `revit_event.py`)
  - `__persistentengine__ = True` so handlers survive after the button click.
  - All exports / temporary-crop transactions run through an `ExternalEvent`
    (`RevitDispatcher`) – the only supported way to touch the model from a
    modeless form.
  - Single instance: clicking the button again brings the open window forward.
  - Bound to one model: title shows the model; "Current window" ranges refuse to
    run if Revit is showing another model; window closes itself when the model closes.
  - Range preview updates live on `ViewActivated`.
  - **Refresh from model** button (keeps ticks).
  - Esc no longer closes the window (it clears the search box).
- **View search & filter** (`view_search.py`, XAML `grpViewFilters`)
  - Drop-downs: View type, Level, Sheet placement (placed / not placed).
  - Grid columns Level + On Sheet (Views mode).
  - Search syntax for both modes: words = AND, `"phrase"`, `-exclude`,
    `field:value` (views: type name level sheet template file;
    sheets: number name file filter). Debounced 220 ms; Enter = now, Esc = clear.
  - Search runs on pre-indexed strings – no Revit API calls while typing.
- Single-click tick boxes; clicking a tick with several rows highlighted
  applies it to all of them; Space toggles highlighted rows.
- Export summary offers **Open Folder**.

## Bugs fixed
1. Progress-bar **Cancel never worked** – `_call_progress` swallowed the cancel exception.
2. Raster PDF quality never applied – `PDFExportQualityType.High` doesn't exist
   (`RasterQuality` is `RasterQualityType`); the error was silently swallowed.
3. Format-tab PDF controls (raster quality, colour, hide crop/scope/ref planes,
   placement, zoom) were **never read** – now mapped to `PDFExportOptions`.
4. "Visible portion" crop used model coordinates in view coordinates – wrong
   crop on sections/elevations/rotated plans. Now uses `CropBox.Transform.Inverse`.
5. Cancel during combined-PDF gathering left the temporary crop on the view.
6. DWG failure (e.g. model with no DWG setups) threw away the PDF results of the
   same run; no setups now falls back to Revit default DWG settings.
7. Switching to Views ignored text already in the Search box.
8. Selection sets in Views mode could tick the **wrong view** (fallback matched
   on view *type*).
9. `ElementId.IntegerValue` (removed in Revit 2026) replaced with a safe helper;
   a DWG that exported fine was being reported as an error on 2026.
10. "Custom File Name" column + search stayed stale after editing the file-name
    sequence; `btnRefreshFormat` was never wired.
11. Combined-PDF name prompt opened behind the progress bar.
12. A cancelled OLE bind no longer hides an already-written DWG from the summary.
13. Stale `__pycache__` (incl. `native_print` with no source) removed; two
    selection-set JSONs found in the root moved into `SelectionSets/`.

## Not changed – please review
Root-level `Default.json, KSS.json, Qtc.json, Qtc_temp.json, My_Format.json,
config.json` are old filename formats nothing reads. Move the ones you want
into `Formats/` (note `KSS.json` vs `Formats/kss.json` collide on Windows).

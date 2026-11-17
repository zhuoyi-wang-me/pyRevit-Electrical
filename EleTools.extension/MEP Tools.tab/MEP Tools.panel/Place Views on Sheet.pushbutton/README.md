# Place Views on Sheet

Places several views on one sheet in a grid: no overlaps, at least 50 mm between viewports
(titles included), and the whole layout centred. Views that don't fit can go on new sheets.

**This tool changes the model.** It creates viewports, and new sheets if you ask for them,
in one transaction. One Ctrl+Z removes all of it.

## Files

```
EleTools.extension/
  MEP Tools.tab/
    MEP Tools.panel/
      bundle.yaml                      <- "Place Views on Sheet" added to the layout
      Place Views on Sheet.pushbutton/
        bundle.yaml                    title, tooltip, author, context: doc-project
        script.py                      choosing views, measuring, placing (all the Revit work)
        pvs_layout.py                  the packing algorithm (plain Python, no Revit)
        config.py                      Shift+click settings
        icon.png, icon.dark.png        96 x 96 button icons (light / dark Revit theme)
        README.md                      this file
```

## How to use it

1. **Pick the sheet.** If a sheet is open, the tool offers it first.
2. **Tick the views.** They're grouped by type. Views already on a sheet show `[on sheet …]`.
   Views ticked in the Project Browser start out ticked.
3. **Enter the spacing** (default 50 mm, remembered for next time).
4. If some views don't fit, choose **Create new sheets** (same title block, named
   `<sheet name> (2)`, numbered `<number>-2` when that number is free) or **Leave them off**.
5. The output window lists what was placed, on which sheet, at what size, and what was skipped and why.

Shift+click the button to set the spacing, the margins inside the title block, and the view order.
Make the margin on the title-strip side large enough to clear it, for example 190 mm on
the right, or 60 mm at the bottom.

## How a view is checked before placement

| Check | Result |
|---|---|
| Already on another sheet (not a legend) | skipped, with that sheet's number |
| A legend that's already on this sheet | skipped. Legends may go on other sheets. |
| `Viewport.CanAddViewToSheet` is False | skipped |
| `Viewport.Create` refuses it (an empty view, for example) | skipped, with Revit's message |
| Bigger than the usable area of the sheet | skipped, with both sizes in mm |

## The placement algorithm

**1. Measure the real extents.** Each view gets a temporary viewport in the middle of the
sheet. After `doc.Regenerate()`, its extent is the union of `GetBoxOutline()` (the view's crop
on the sheet) and `GetLabelOutline()` (its title, Revit 2022+). The offset from the box centre
to the extent's lower-left corner is stored, because `Viewport.Create` and `SetBoxCenter` work
with the box centre.

**2. Find the usable area.** This is the title block's bounding box on the sheet minus the four
margins. If there's no title block, the sheet outline is used, or A1 as a last resort. Anything
already on the sheet (viewports with their titles, schedules) counts as an obstacle. Revision
schedules that belong to the title block don't count.

**3. Pack the views row by row** (`pvs_layout.pack`):
- The cursor starts at the top-left of the usable area.
- Each view goes at the cursor, its top on the row's top line. If that would come within the
  spacing of an obstacle or an already-placed view, it slides right past it.
- When the remaining width of the row isn't enough, a new row starts one spacing below the
  lowest view of the current row. If obstacles block a whole row at that height, the row drops
  below them.
- A view that no longer fits above the bottom edge waits for the next sheet. The views after it
  are still tried, because a smaller one may fit at the end of a row.
- By default the tallest views go first (the standard shelf-packing heuristic), so each row is
  about as tall as its views. Choose "In the order listed" in the settings to keep your order.

**4. Centre the layout.** The whole block of placed views moves to the centre of the usable
area. If that would bring it within the spacing of an obstacle, the tool tries centring
left-right only, then up-down only, and otherwise leaves the block at the top-left. The report
says which was used.

**5. Check, then place.** `pvs_layout.check` confirms that every view is inside the area and at
least the spacing from every other view and obstacle. If the check fails, the transaction is
rolled back and nothing is placed. Each viewport then gets `SetBoxCenter(slot + offset)`.
A viewport can't move between sheets, so a view going to a new sheet is deleted and created
again there.

**6. Overflow.** Views left over go to a new sheet with the same title block and are packed
the same way, repeating until all are placed. If a view doesn't fit even on an empty sheet,
it's reported instead of looping forever.

## Compatibility

- **Revit 2022–2025:** element ids are read through a helper (`ElementId.Value` on 2024+,
  `IntegerValue` before, and it was removed in 2026). `GetLabelOutline` is guarded.
- **pyRevit engines:** works on IronPython 2.7 and CPython. No f-strings, and view types are
  matched by enum name, not `int(enum)`.

## Deploying

1. The button is part of `EleTools.extension`. If that extension is already installed in
   pyRevit (Extensions → or `pyrevit extend ui EleTools <path or repo>`), press
   **pyRevit → Reload** and the button appears in **MEP Tools → MEP Tools** panel.
2. For a new machine: copy the `EleTools.extension` folder into a folder of your own, then add
   that folder in **pyRevit → Settings → Custom Extension Directories** and press Reload.
3. To ship only this button in another extension, copy `Place Views on Sheet.pushbutton` into
   any `.panel` folder and add `- Place Views on Sheet` to that panel's `bundle.yaml` layout.
4. Icons: `icon.png` (light theme) and `icon.dark.png` (dark theme), 96 × 96 like the other
   EleTools buttons. pyRevit picks the one that matches the Revit theme.
